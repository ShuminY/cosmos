"""独立 OCR worker 脚本 - 在子进程中运行，避免 onnxruntime/numpy 不兼容崩掉主进程。

用法：python ocr_worker.py <image_path1> <image_path2> ...
输出：JSON 数组到 stdout，每项 {"title": str, "code": str}
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path


def _run_ocr(image_paths: list[Path]) -> list[dict]:
    from rapidocr_onnxruntime import RapidOCR
    from PIL import Image
    import numpy as np

    engine = RapidOCR()

    TITLE_LABELS = ("图名", "图纸名称", "名称", "设计图名", "图 名")
    CODE_LABELS = ("图号", "图纸编号", "图 号", "编号", "图别")
    code_re = re.compile(r"\b([A-Za-z]{1,3}[- ]?\d{1,3})\b")
    OCR_MAX_LONG = 3000

    results = []
    for src in image_paths:
        code = ""
        title = ""
        try:
            with Image.open(src) as img:
                w, h = img.size
                box = (int(w * 0.65), 0, w, h)
                crop = img.convert("RGB").crop(box)
                cw, ch = crop.size
                scale = 2.0
                long_side = max(cw, ch) * scale
                if long_side > OCR_MAX_LONG:
                    scale = OCR_MAX_LONG / max(cw, ch)
                new_w = max(1, int(cw * scale))
                new_h = max(1, int(ch * scale))
                if new_w != cw or new_h != ch:
                    crop = crop.resize((new_w, new_h), Image.LANCZOS)
                img_arr = np.array(crop)

            ocr_result, _ = engine(img_arr)
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
                cx, cy = sum(xs) / 4, sum(ys) / 4
                items.append((cx, cy, text, conf))

            if not items:
                results.append({"title": "", "code": ""})
                continue

            items_sorted = sorted(items, key=lambda x: x[1])
            rows: list[list[tuple]] = []
            row_y = -1e9
            for it in items_sorted:
                cy = it[1]
                if cy - row_y > 25:
                    rows.append([it])
                    row_y = cy
                else:
                    rows[-1].append(it)

            for row in rows:
                row.sort(key=lambda x: x[0])

            best_code_score = -1.0
            best_title_score = -1.0
            crop_width = crop.width

            for row in rows:
                for cx, cy, text, conf in row:
                    for m in code_re.finditer(text):
                        candidate = m.group(1).replace(" ", "").upper()
                        if len(candidate) < 2 or len(candidate) > 10:
                            continue
                        score = conf + cx / max(crop_width, 1) * 0.5
                        if score > best_code_score:
                            best_code_score = score
                            code = candidate

                for idx, (cx, cy, text, conf) in enumerate(row):
                    is_label = any(lab in text for lab in TITLE_LABELS)
                    if is_label:
                        candidates_after = [
                            t for j, (_, _, t, _) in enumerate(row) if j > idx
                        ]
                        if candidates_after:
                            name = "".join(candidates_after).strip()
                            name = name.lstrip(":：").strip()
                            cjk_count = sum(1 for c in name if '一' <= c <= '鿿')
                            if cjk_count >= 2 and len(name) <= 40:
                                title = name
                                best_title_score = 99.0
                                break

                if best_title_score >= 99.0:
                    break

            if not title:
                best_row_cjk = 0
                for row in rows:
                    row_text = "".join(t for _, _, t, _ in row)
                    cjk_count = sum(1 for c in row_text if '一' <= c <= '鿿')
                    has_code_label = any(lab in row_text for lab in CODE_LABELS)
                    if cjk_count > best_row_cjk and not has_code_label \
                            and 4 <= len(row_text) <= 40 and cjk_count >= 3:
                        best_row_cjk = cjk_count
                        clean = code_re.sub('', row_text).strip()
                        clean = clean.rstrip(":：-—").strip()
                        if 3 <= len(clean) <= 40:
                            title = clean
        except Exception:
            pass

        results.append({"title": title, "code": code})

    return results


def main():
    if len(sys.argv) < 2:
        print(json.dumps([]))
        return

    paths = [Path(p) for p in sys.argv[1:]]
    try:
        results = _run_ocr(paths)
    except Exception:
        # 任何异常（导入失败、OCR 崩溃等）都返回空结果
        results = [{"title": "", "code": ""} for _ in paths]

    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
