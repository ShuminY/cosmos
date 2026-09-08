"""独立 OCR worker 脚本 - 在子进程中运行，避免 onnxruntime/numpy 不兼容崩掉主进程。

用法：python ocr_worker.py <image_path1> <image_path2> ...
输出：JSON 数组到 stdout，每项 {"title": str, "code": str}
诊断信息（导入失败、单页异常）打到 stderr；依赖缺失时 exit code=2，
让调用方能区分"OCR 没识别到内容"和"OCR 根本没跑起来"。

提取策略（按优先级）：
1. 标签锚定：找"图名/DRAWING TITLE"、"图号/图纸编号/DRAWING NO"标签，
   取其下方（值在标签下面的竖排单元格）或同行右侧（横排单元格）的值。
   图号支持字母+数字（P01/GT-09）和纯数字（001）。
2. 兜底：整右条带正则抓字母+数字图号；图名取底部区域字号最大的中文行。
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path


def _norm(text: str) -> str:
    """标签匹配用：去空白、拉丁字母大写。"""
    return re.sub(r"\s+", "", text).upper()


# 归一化后做子串匹配的标签词
TITLE_LABELS = ("图名", "图纸名称", "设计图名", "DRAWINGTITLE")
CODE_LABELS = ("图号", "图纸编号", "图别", "DRAWINGNO")
# 值采集时遇到这些标签就停（说明越界到下一个单元格了）
STOP_LABELS = TITLE_LABELS + CODE_LABELS + (
    "比例", "SCALE", "日期", "DATE", "设计阶段", "STATUS",
    "建设单位", "项目名称", "签字", "项目负责", "设计", "制图", "审核",
)

# 图号值：字母+数字（P01 / GT-09 / MB03 / J1-05）或纯数字 2~4 位（001 / 12）
_CODE_ALPHA_RE = re.compile(r"^[A-Za-z]{1,4}-?\d{1,3}[A-Za-z]?$")
_CODE_NUMERIC_RE = re.compile(r"^\d{2,4}$")
# 兜底用：文本中嵌入的字母+数字图号
_CODE_FIND_RE = re.compile(r"\b([A-Za-z]{1,4}-?\d{1,3})\b")

OCR_MAX_LONG = 3000


def _clean_code(text: str) -> str:
    """把 OCR 文本规整成图号；不像图号返回空串。"""
    t = re.sub(r"\s+", "", text).strip().upper().lstrip(":：")
    if not t or len(t) > 10:
        return ""
    # 排除比例/日期/标高等（含 : / @ . 的一定不是图号）
    if re.search(r"[:/@.]", t):
        return ""
    # OCR 常把 0 读成 O（如 P00→POO），O→0 后重试字母+数字模式
    for variant in (t, t.replace("O", "0")):
        if _CODE_ALPHA_RE.match(variant):
            return variant
    if _CODE_NUMERIC_RE.match(t):
        # 4 位数字大概率是年份（2024），只收 19/20 开头的以外的
        if len(t) == 4 and t.startswith(("19", "20")):
            return ""
        return t
    return ""


def _clean_title(text: str) -> str:
    t = text.strip().lstrip(":：").strip()
    return t


def _cjk_count(text: str) -> int:
    return sum(1 for c in text if "一" <= c <= "鿿")


def _is_label(norm_text: str, labels) -> bool:
    return any(lab in norm_text for lab in labels)


def _value_below(label_item, items, line_h: float) -> list:
    """取标签正下方同列的候选项（竖排单元格：值在标签下面），按 y 排序。"""
    lx0, ly0, lx1, ly1 = label_item["bbox"]
    lw = max(lx1 - lx0, 1.0)
    out = []
    for it in items:
        if it is label_item:
            continue
        x0, y0, x1, y1 = it["bbox"]
        cx = (x0 + x1) / 2
        # 在标签下方 0.2~8 倍行高内
        if not (ly1 - 0.2 * line_h < (y0 + y1) / 2 < ly1 + 8 * line_h):
            continue
        # 水平方向与标签列大致对齐（允许标签比值短/长）
        if lx0 - 1.2 * lw <= cx <= lx1 + 1.2 * lw:
            out.append(it)
    out.sort(key=lambda it: it["bbox"][1])
    return out


def _value_right(label_item, row_items) -> list:
    """取标签同行右侧的候选项（横排单元格：值在标签右边）。"""
    lx1 = label_item["bbox"][2]
    out = [it for it in row_items if it["bbox"][0] >= lx1 - 2]
    out.sort(key=lambda it: it["bbox"][0])
    return out


def _extract_from_items(items, crop_h: int, allow_fallback: bool = True) -> dict:
    """items: [{text, conf, bbox}]，返回 {"title": str, "code": str}。

    allow_fallback=False 时只做标签锚定（用于右下角高分辨率二次识别，
    避免兜底规则把项目名称/建设单位误当图名）。
    """
    title = ""
    code = ""
    if not items:
        return {"title": title, "code": code}

    heights = sorted(it["bbox"][3] - it["bbox"][1] for it in items)
    line_h = heights[len(heights) // 2] if heights else 20.0
    line_h = max(line_h, 8.0)

    # 按行分组（同行容差 0.6 倍行高），行内按 x 排序
    items_sorted = sorted(items, key=lambda it: (it["bbox"][1] + it["bbox"][3]) / 2)
    rows: list[list[dict]] = []
    row_y = -1e18
    for it in items_sorted:
        cy = (it["bbox"][1] + it["bbox"][3]) / 2
        if cy - row_y > 0.6 * line_h:
            rows.append([it])
            row_y = cy
        else:
            rows[-1].append(it)
    for row in rows:
        row.sort(key=lambda it: it["bbox"][0])

    # ---- 1) 标签锚定 ----
    for it in items:
        n = _norm(it["text"])
        if not title and _is_label(n, TITLE_LABELS):
            # 先找下方（竖排单元格），再找同行右侧（横排单元格）
            below = _value_below(it, items, line_h)
            parts = []
            for cand in below:
                cn = _norm(cand["text"])
                if _is_label(cn, STOP_LABELS):
                    break
                t = _clean_title(cand["text"])
                # 值里混入英文标签（如 OCR 把 DRAWING TITLE 读进值）则跳过
                if not t or _clean_code(t):
                    continue
                if _cjk_count(t) < 1:
                    continue
                parts.append(t)
                if len(parts) >= 3:  # 图名最多三行（长图名常被 OCR 拆行）
                    break
            if parts:
                title = "".join(parts)[:40]
            else:
                row = next((r for r in rows if it in r), [])
                right = _value_right(it, row)
                name = "".join(_clean_title(c["text"]) for c in right)
                name = name.lstrip(":：").strip()
                if _cjk_count(name) >= 2 and len(name) <= 40:
                    title = name

        if not code and _is_label(n, CODE_LABELS):
            for cand in _value_below(it, items, line_h):
                c = _clean_code(cand["text"])
                if c:
                    code = c
                    break
            if not code:
                row = next((r for r in rows if it in r), [])
                for cand in _value_right(it, row):
                    c = _clean_code(cand["text"])
                    if c:
                        code = c
                        break
        if title and code:
            break

    # ---- 2) 兜底：图号正则（只收字母+数字，避免误吃尺寸/日期）----
    if not code and allow_fallback:
        best = -1.0
        crop_w = max(max(it["bbox"][2] for it in items), 1)
        for it in items:
            # 跳过比例/图幅行（"1:40@A2" 里的 A2 不是图号）
            if "@" in it["text"] or re.search(r"\d\s*[:：]\s*\d", it["text"]):
                continue
            if _is_label(_norm(it["text"]), ("比例", "SCALE")):
                continue
            for m in _CODE_FIND_RE.finditer(it["text"]):
                cand = _clean_code(m.group(1))
                if not cand or not _CODE_ALPHA_RE.match(cand):
                    continue
                cx = (it["bbox"][0] + it["bbox"][2]) / 2
                cy = (it["bbox"][1] + it["bbox"][3]) / 2
                score = it["conf"] + cx / crop_w * 0.5 + cy / max(crop_h, 1) * 0.3
                if score > best:
                    best = score
                    code = cand

    # ---- 3) 兜底：图名取底部 55% 区域里字号最大的中文行 ----
    if not title and allow_fallback:
        best_h = 0.0
        best_text = ""
        for row in rows:
            row_items = [
                it for it in row
                if (it["bbox"][1] + it["bbox"][3]) / 2 > crop_h * 0.45
            ]
            if not row_items:
                continue
            row_text = "".join(it["text"] for it in row_items)
            rn = _norm(row_text)
            if _is_label(rn, STOP_LABELS):
                continue
            if _cjk_count(row_text) < 3 or not (4 <= len(row_text) <= 40):
                continue
            if _CODE_FIND_RE.search(row_text) and _cjk_count(row_text) < 4:
                continue
            # 排除明显是标注的行（尺寸/材料说明）
            if re.search(r"mm|直径|标高|@|1:", row_text):
                continue
            avg_h = sum(it["bbox"][3] - it["bbox"][1] for it in row_items) / len(row_items)
            if avg_h > best_h:
                clean = _CODE_FIND_RE.sub("", row_text).strip().rstrip(":：-—").strip()
                if 3 <= len(clean) <= 40:
                    best_h = avg_h
                    best_text = clean
        title = best_text

    return {"title": title, "code": code}


def _ocr_region(engine, img, box, max_scale: float):
    """裁剪 → 放大（不超过 OCR_MAX_LONG 长边）→ OCR，返回 (items, crop_height)。"""
    import numpy as np
    from PIL import Image

    crop = img.convert("RGB").crop(box)
    cw, ch = crop.size
    scale = min(max_scale, OCR_MAX_LONG / max(cw, ch))
    new_w, new_h = max(1, int(cw * scale)), max(1, int(ch * scale))
    if (new_w, new_h) != (cw, ch):
        crop = crop.resize((new_w, new_h), Image.LANCZOS)

    ocr_result, _ = engine(np.array(crop))
    if ocr_result is None:
        ocr_result = []

    items = []
    for item in ocr_result:
        if len(item) < 3:
            continue
        box_pts = item[0]
        text = str(item[1]).strip()
        conf = float(item[2]) if len(item) > 2 else 0.5
        if not text or conf < 0.3:
            continue
        xs = [p[0] for p in box_pts]
        ys = [p[1] for p in box_pts]
        items.append({
            "text": text,
            "conf": conf,
            "bbox": (min(xs), min(ys), max(xs), max(ys)),
        })
    return items, crop.height


def _run_ocr(image_paths: list[Path]) -> list[dict]:
    from rapidocr_onnxruntime import RapidOCR
    from PIL import Image

    engine = RapidOCR()

    results = []
    for src in image_paths:
        try:
            with Image.open(src) as img:
                w, h = img.size
                # 第一遍：右下角角落（右 40% × 下 45%）高放大倍数。
                # 标题栏大多在右下角，角落裁剪后文字相对更大，
                # 长图名（如"客餐厅厨房立面图（一）"）才不会被整页压缩吃掉。
                corner_items, corner_h = _ocr_region(
                    engine, img, (int(w * 0.60), int(h * 0.55), w, h),
                    max_scale=3.0,
                )
            res = _extract_from_items(corner_items, corner_h, allow_fallback=False)

            # 第二遍：右侧条带全高（标题栏在右中部的图纸，或角落没认全时兜底）
            if not (res["title"] and res["code"]):
                with Image.open(src) as img:
                    w, h = img.size
                    strip_items, strip_h = _ocr_region(
                        engine, img, (int(w * 0.65), 0, w, h),
                        max_scale=2.0,
                    )
                res2 = _extract_from_items(strip_items, strip_h, allow_fallback=True)
                if not res["title"]:
                    res["title"] = res2["title"]
                if not res["code"]:
                    res["code"] = res2["code"]

            results.append(res)
        except Exception as e:
            print(f"[ocr_worker] {src.name}: {e}", file=sys.stderr)
            results.append({"title": "", "code": ""})

    return results


def main():
    if len(sys.argv) < 2:
        print(json.dumps([]))
        return

    paths = [Path(p) for p in sys.argv[1:]]
    try:
        results = _run_ocr(paths)
    except ImportError as e:
        # 依赖没装：exit 2 + stderr，让调用方知道 OCR 根本没跑起来
        print(f"[ocr_worker] 依赖缺失: {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"[ocr_worker] 运行失败: {e}", file=sys.stderr)
        results = [{"title": "", "code": ""} for _ in paths]

    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
