"""图纸分析视图 - 查看历史图纸分析和新增图纸分析。"""
from __future__ import annotations
import base64
import html
import json
import re
import subprocess
import time
import uuid
from datetime import datetime
from functools import lru_cache
from pathlib import Path, PurePosixPath
from zipfile import ZipFile
import xml.etree.ElementTree as ET

import streamlit as st
from PIL import Image, ImageDraw, ImageFont

# 人工批注在图上拖拽画框（可选依赖，缺失时回退为纯文本批注）
try:
    # streamlit-drawable-canvas 0.9.3 与新版 Streamlit 有两处不兼容，这里做兼容适配：
    #  1) 该库从 streamlit.elements.image 引 image_to_url，但 Streamlit ≥1.34 已把该符号
    #     迁移到 streamlit.elements.lib.image_utils
    #  2) Streamlit ≥1.55 的 image_to_url 第二个参数变成 LayoutConfig 对象，而 canvas
    #     仍按老签名传入一个 int width
    from streamlit.elements import image as _st_image
    if not hasattr(_st_image, "image_to_url"):
        try:
            from streamlit.elements.lib.image_utils import image_to_url as _image_to_url_new

            try:
                from streamlit.elements.lib.layout_utils import LayoutConfig as _LayoutConfig
            except ImportError:
                _LayoutConfig = None

            def _image_to_url_compat(image, width, clamp, channels, output_format, image_id):
                """兼容 canvas 的老式位置参数调用（width 是 int）→ 新 API（需要 LayoutConfig）。"""
                if _LayoutConfig is not None and isinstance(width, int):
                    layout_config = _LayoutConfig(width=width)
                else:
                    layout_config = width
                return _image_to_url_new(image, layout_config, clamp, channels, output_format, image_id)

            _st_image.image_to_url = _image_to_url_compat  # type: ignore[attr-defined]
        except ImportError:
            pass
    from streamlit_drawable_canvas import st_canvas
    CANVAS_AVAILABLE = True
except ImportError:
    st_canvas = None
    CANVAS_AVAILABLE = False

# PDF处理依赖：优先使用 PyMuPDF，避免本地环境必须安装 Poppler
try:
    import fitz  # PyMuPDF
    PYMUPDF_AVAILABLE = True
except ImportError:
    fitz = None
    PYMUPDF_AVAILABLE = False

try:
    from pdf2image import convert_from_path
    PDF2IMAGE_AVAILABLE = True
except ImportError:
    PDF2IMAGE_AVAILABLE = False

from src.chatbot import (
    get_available_providers, get_default_provider, get_image_messages,
    list_chat_sessions, vision_chat,
)
from src.db import session, ChatSession, Document, Project
from src.prompts import load_prompt
from src import drawing_rag
from src.storage import documents_dir, project_dir
from src.time_utils import beijing_timestamp, format_beijing, now_utc


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
RULES_WORKBOOK_NAME = "审图依据-审查规则库.xlsx"
REVIEW_SESSION_PREFIX = "图纸审核："
ANALYSIS_SESSION_PREFIX = "图纸分析："


def _user_id() -> int | None:
    """获取当前用户ID."""
    user = st.session_state.get("user") or {}
    return user.get("id")


def _save_uploaded_drawing(project_id: int, uploaded_file) -> int:
    """保存新增分析页上传的图纸文件，并写入Document记录."""
    target_dir = documents_dir(project_id, "03_drawings")
    dest = target_dir / uploaded_file.name
    if dest.exists():
        stem = dest.stem
        suffix = dest.suffix
        timestamp = beijing_timestamp()
        dest = target_dir / f"{stem}_{timestamp}{suffix}"
    dest.write_bytes(uploaded_file.getbuffer())

    with session() as s:
        doc = Document(
            project_id=project_id,
            category="03_drawings",
            filename=dest.name,
            path=str(dest.relative_to(project_dir(project_id))),
            size_bytes=dest.stat().st_size,
            uploaded_by_id=_user_id(),
        )
        s.add(doc)
        s.flush()
        doc_id = doc.id
        s.commit()
        return doc_id


def _extract_labels_after_upload(project_id: int, doc_id: int) -> tuple[int, str | None]:
    """上传成功后预处理 PDF→PNG 并自动提取页面名称（图名+图号）。

    返回 (已识别命名的页数, 错误信息)。任何异常都被吞掉、只返回错误串，不影响主流程。
    """
    try:
        with session() as s:
            doc = s.query(Document).filter(Document.id == doc_id).first()
            if not doc:
                return 0, "上传后找不到文档记录"
            # 提前 detach，后续函数会自开 session
            s.expunge(doc)

        image_paths, err = _prepare_drawing_images(doc, project_id)
        if err:
            return 0, err
        if not image_paths:
            return 0, "预处理后未生成图片"

        labels = _ensure_page_labels(doc, project_id, image_paths, use_vision=True, force=True)
        named = sum(1 for x in labels if (x.get("title") or x.get("code")))
        return named, None
    except Exception as e:
        return 0, str(e)


def _get_drawing_documents(project: Project):
    """获取项目中的所有图纸文档."""
    with session() as s:
        docs = (
            s.query(Document)
            .filter(
                Document.project_id == project.id,
                Document.category == "03_drawings"
            )
            .order_by(Document.uploaded_at.desc())
            .all()
        )
        return docs


def _doc_file_path(project_id: int, doc: Document) -> Path:
    """获取图纸文件绝对路径."""
    return Path("data") / "projects" / str(project_id) / doc.path


def _parse_analysis_data(doc: Document):
    """解析图纸分析数据."""
    if not doc.analysis_data_json:
        return {}
    try:
        return json.loads(doc.analysis_data_json)
    except Exception:
        return {}


def _convert_pdf_to_images(pdf_path: Path, project_id: int, doc_id: int) -> list[Path]:
    """将PDF文件转换为PNG图片."""
    output_dir = Path("data") / "projects" / str(project_id) / "pdf_conversions" / str(doc_id)
    output_dir.mkdir(parents=True, exist_ok=True)

    existing_pages = sorted(output_dir.glob("page_*.png"))
    if existing_pages:
        return existing_pages

    if PYMUPDF_AVAILABLE:
        pdf_doc = fitz.open(str(pdf_path))
        try:
            for page_index in range(pdf_doc.page_count):
                page = pdf_doc.load_page(page_index)
                pix = page.get_pixmap(dpi=200, alpha=False)
                pix.save(str(output_dir / f"page_{page_index + 1:03d}.png"))
        finally:
            pdf_doc.close()
        return sorted(output_dir.glob("page_*.png"))

    if PDF2IMAGE_AVAILABLE:
        try:
            convert_from_path(
                str(pdf_path),
                dpi=200,
                fmt="png",
                thread_count=4,
                use_pdftocairo=True,
                output_folder=str(output_dir),
                output_file="page_",
            )
        except Exception as e:
            raise RuntimeError(
                "PDF 转 PNG 失败。建议安装 PyMuPDF，或安装 Poppler 并确保 pdfinfo/pdftoppm 在 PATH 中。"
                f" 原始错误: {e}"
            ) from e
        return sorted(output_dir.glob("page_*.png"))

    raise RuntimeError("缺少PDF渲染依赖：请安装 PyMuPDF，或安装 pdf2image + Poppler")


def _prepare_drawing_images(doc: Document, project_id: int) -> tuple[list[Path], str | None]:
    """将图纸预处理为可用于视觉模型的图片路径."""
    file_path = _doc_file_path(project_id, doc)
    if not file_path.exists():
        return [], f"图纸文件不存在: {file_path}"

    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        try:
            image_paths = _convert_pdf_to_images(file_path, project_id, doc.id)
        except Exception as e:
            return [], str(e)
        if not image_paths:
            return [], "PDF 转 PNG 后未生成图片"
        return image_paths, None

    if suffix in IMAGE_SUFFIXES:
        return [file_path], None

    return [], f"暂不支持该图纸格式: {suffix or '未知'}"


# ============ 页面标题（图名 + 图号）提取 ============
# 常见图号形式：P00 / P-02 / A-01 / GT-09 / J1-05 / M-101 等
_SHEET_CODE_PATTERN = re.compile(
    r"\b([A-Z]{1,3}\d{0,2}[\-\s]?\d{1,3})\b"
)


def _extract_pdf_sheet_codes(pdf_path: Path) -> list[str]:
    """从多页 PDF 的文本层提取每页图号，返回长度==page_count 的字符串列表（缺失为空字符串）。

    只依赖 PyMuPDF；PDF 中文名常有字体映射问题会读成乱码，所以图名不在这里抓，
    另外交由视觉 LLM 处理。
    """
    if not PYMUPDF_AVAILABLE:
        return []
    try:
        pdf_doc = fitz.open(str(pdf_path))
    except Exception:
        return []

    results: list[str] = []
    try:
        for page_index in range(pdf_doc.page_count):
            page = pdf_doc.load_page(page_index)
            w, h = page.rect.width, page.rect.height
            code = ""
            best_score = -1.0
            try:
                text_dict = page.get_text("dict")
            except Exception:
                results.append("")
                continue

            for block in text_dict.get("blocks", []):
                if block.get("type") != 0:
                    continue
                x0, y0, x1, y1 = block["bbox"]
                cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                # 图号一般在页面右下角；用矩形距离右下角的比例作为得分（越靠右下越高）
                if cy < h * 0.55 or cx < w * 0.55:
                    continue
                text = "".join(
                    span["text"]
                    for line in block.get("lines", [])
                    for span in line.get("spans", [])
                ).strip()
                if not text:
                    continue
                for match in _SHEET_CODE_PATTERN.finditer(text):
                    candidate = match.group(1).replace(" ", "").replace("-", "-").upper()
                    # 得分：靠右下 + 单元格短（长文本里含相似模式的概率高）
                    score = (cx / w) + (cy / h) - min(1.0, len(text) / 40)
                    if score > best_score:
                        best_score = score
                        code = candidate
            results.append(code)
    finally:
        pdf_doc.close()
    return results


def _extract_page_titles_via_vision(image_paths: list[Path], provider: str | None = None) -> list[dict]:
    """用视觉模型批量提取每页的图名（右下角标题栏）。

    每张图裁剪右下 40%×45% 区域后一起发给视觉模型，返回长度==len(image_paths) 的 dict 列表，
    每个 dict 形如 {"title": "A户型平面图", "code": "P00"}（缺失字段为空串）。
    """
    if not image_paths:
        return []

    from src.chatbot import provider_openai_vision, load_provider_config, get_default_provider
    if provider is None:
        provider = get_default_provider()
    provider_config = load_provider_config(provider)

    # 生成裁剪图；写到 pdf_conversions 附近的临时目录
    crops: list[Path] = []
    for src in image_paths:
        try:
            with Image.open(src) as img:
                w, h = img.size
                # 右下 40% × 45%
                box = (int(w * 0.60), int(h * 0.55), w, h)
                crop = img.convert("RGB").crop(box)
                crop_path = src.parent / f"__titleblock_{src.stem}.png"
                crop.save(crop_path, format="PNG")
                crops.append(crop_path)
        except Exception:
            crops.append(src)  # 兜底：整页也行，只是 token 更多

    prompt = (
        "以下每张图片都是一张建筑/装饰图纸右下角标题栏的裁剪。请按顺序输出每张图上"
        "可读取的“图名”和“图号”。图号通常是字母+数字（如 P00、P-02、A-01、GT-09），"
        "图名是紧邻图号的中文短语（如“A户型平面图”“客厅立面图”“天花吊顶图”）。\n"
        "严格输出 JSON 数组，长度与图片数一致，每项是 {\"title\": \"...\", \"code\": \"...\"}。"
        "无法识别时对应字段填空字符串。不要输出多余说明，也不要用 markdown 代码块包裹。"
    )
    answer, err = provider_openai_vision(prompt, provider_config, [str(p) for p in crops])
    # 清理临时裁剪
    for p in crops:
        if p.name.startswith("__titleblock_"):
            try:
                p.unlink()
            except Exception:
                pass
    if err or not answer:
        return [{"title": "", "code": ""} for _ in image_paths]

    match = re.search(r"\[.*\]", answer, flags=re.DOTALL)
    if not match:
        return [{"title": "", "code": ""} for _ in image_paths]
    try:
        parsed = json.loads(match.group(0))
    except Exception:
        return [{"title": "", "code": ""} for _ in image_paths]

    out: list[dict] = []
    for i in range(len(image_paths)):
        item = parsed[i] if i < len(parsed) and isinstance(parsed[i], dict) else {}
        out.append({
            "title": str(item.get("title", "") or "").strip(),
            "code": str(item.get("code", "") or "").strip().upper(),
        })
    return out


def _load_page_labels(analysis_data: dict) -> list[dict]:
    """从 analysis_data 读取页面标签列表；缺失时返回空列表。

    存储格式：analysis_data["page_labels"] = [{"title": "...", "code": "..."}, ...]
    """
    labels = analysis_data.get("page_labels")
    if not isinstance(labels, list):
        return []
    return [
        {"title": str(x.get("title", "") if isinstance(x, dict) else "").strip(),
         "code": str(x.get("code", "") if isinstance(x, dict) else "").strip()}
        for x in labels
    ]


def _format_page_label(labels: list[dict], page_number: int) -> str:
    """将某页的 (title, code) 拼成显示后缀，如 "A户型平面区域图P00"。都没有返回空串。"""
    idx = page_number - 1
    if idx < 0 or idx >= len(labels):
        return ""
    title = labels[idx].get("title", "") or ""
    code = labels[idx].get("code", "") or ""
    if title and code:
        return f"{title}{code}"
    return title or code


def _save_page_labels(doc_id: int, labels: list[dict]):
    """把页面标签结果写回 Document.analysis_data_json.page_labels."""
    with session() as s:
        doc_db = s.query(Document).filter(Document.id == doc_id).first()
        if not doc_db:
            return
        data = {}
        if doc_db.analysis_data_json:
            try:
                data = json.loads(doc_db.analysis_data_json)
            except json.JSONDecodeError:
                data = {}
        data["page_labels"] = labels
        doc_db.analysis_data_json = json.dumps(data, ensure_ascii=False, indent=2)
        s.commit()


def _ensure_page_labels(doc: Document, project_id: int, image_paths: list[Path],
                       use_vision: bool = True, provider: str | None = None,
                       force: bool = False) -> list[dict]:
    """确保 Document 有 page_labels；没有则先用 PDF 文本层提图号，再用视觉模型补图名。

    - use_vision=False 时跳过 LLM 调用，只做文本层图号提取
    - force=True 时忽略缓存重跑
    - 返回最终的 labels 列表；空图纸/失败返回 []
    """
    if not image_paths:
        return []

    data = _parse_analysis_data(doc)
    cached = _load_page_labels(data)
    if not force and cached and len(cached) == len(image_paths):
        return cached

    total = len(image_paths)
    labels = [{"title": "", "code": ""} for _ in range(total)]

    # 1) 从 PDF 文本层抓图号
    file_path = _doc_file_path(project_id, doc)
    if file_path.suffix.lower() == ".pdf" and PYMUPDF_AVAILABLE:
        codes = _extract_pdf_sheet_codes(file_path)
        for i in range(min(total, len(codes))):
            if codes[i]:
                labels[i]["code"] = codes[i]

    # 2) 视觉模型抓图名
    if use_vision:
        try:
            vision_labels = _extract_page_titles_via_vision(image_paths, provider=provider)
        except Exception:
            vision_labels = []
        for i in range(min(total, len(vision_labels))):
            if vision_labels[i].get("title"):
                labels[i]["title"] = vision_labels[i]["title"]
            # 视觉模型抓到的图号只在文本层没读到时才用（PDF 文本层更权威）
            if not labels[i]["code"] and vision_labels[i].get("code"):
                labels[i]["code"] = vision_labels[i]["code"]

    _save_page_labels(doc.id, labels)
    return labels


def _format_drawing_prompt(doc: Document, analysis_type: list[str], custom_prompt: str) -> str:
    """构建图纸分析提示词（模板来自 prompts/drawing_analysis.md）."""
    focus = "、".join(analysis_type) if analysis_type else "综合图纸分析"
    extra = f"\n\n用户补充要求：{custom_prompt}" if custom_prompt else ""
    return load_prompt(
        "drawing_analysis",
        filename=doc.filename,
        focus=focus,
        extra=extra,
    )


def _column_index(cell_ref: str) -> int:
    """将Excel列名转为0-based索引."""
    letters = re.sub(r"[^A-Z]", "", cell_ref.upper())
    idx = 0
    for ch in letters:
        idx = idx * 26 + ord(ch) - ord("A") + 1
    return idx - 1


def _read_xlsx_sheet_rows(path: Path, sheet_name: str) -> list[list[str]]:
    """使用标准库读取xlsx指定sheet的文本行，避免运行环境缺openpyxl时报错."""
    ns = {
        "a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }
    with ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall("a:si", ns):
                shared.append("".join(t.text or "" for t in si.findall(".//a:t", ns)))

        workbook = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        rel_map = {r.attrib["Id"]: r.attrib["Target"] for r in rels}

        sheet_path = None
        for sheet in workbook.findall("a:sheets/a:sheet", ns):
            if sheet.attrib.get("name") != sheet_name:
                continue
            rid = sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
            target = rel_map[rid]
            sheet_path = target.lstrip("/") if target.startswith("/") else str(PurePosixPath("xl") / target)
            if sheet_path.startswith("xl/xl/"):
                sheet_path = sheet_path[3:]
            break

        if not sheet_path:
            return []

        sheet_root = ET.fromstring(z.read(sheet_path))

        def cell_text(cell) -> str:
            value = cell.find("a:v", ns)
            if value is None:
                inline = cell.find("a:is", ns)
                if inline is not None:
                    return "".join(t.text or "" for t in inline.findall(".//a:t", ns)).strip()
                return ""
            text = value.text or ""
            if cell.attrib.get("t") == "s" and text.isdigit():
                idx = int(text)
                return shared[idx].strip() if idx < len(shared) else text
            return text.strip()

        rows = []
        for row in sheet_root.findall("a:sheetData/a:row", ns):
            cells = row.findall("a:c", ns)
            values = []
            for cell in cells:
                idx = _column_index(cell.attrib.get("r", "A"))
                while len(values) < idx:
                    values.append("")
                values.append(cell_text(cell))
            rows.append(values)
        return rows


def _format_review_prompt(doc: Document, rules: list[dict], cases: list[dict], custom_prompt: str) -> str:
    """构建基于规则库+人工案例的图纸审核提示词（模板来自 prompts/drawing_review.md）."""
    rules_md = drawing_rag.rules_to_markdown(rules)
    cases_md = drawing_rag.cases_to_markdown(cases)
    extra = f"\n\n用户补充要求：{custom_prompt}" if custom_prompt else ""
    return load_prompt(
        "drawing_review",
        filename=doc.filename,
        rules_md=rules_md,
        cases_md=cases_md,
        extra=extra,
    )


def _render_rule_kb_status(project: Project):
    """展示审图规则/人工案例知识库状态，并提供从 Excel 同步规则的入口."""
    rule_count = drawing_rag.count_rules(project.id)
    case_count = drawing_rag.count_cases(project.id)

    col_a, col_b, col_c = st.columns([1, 1, 2])
    col_a.metric("审图规则（已入库）", rule_count)
    col_b.metric("人工案例（已入库）", case_count)

    with col_c:
        xlsx_path = Path("data") / "projects" / str(project.id) / RULES_WORKBOOK_NAME
        has_xlsx = xlsx_path.exists()
        if st.button(
            "🔄 同步规则到知识库",
            key=f"sync_rules_{project.id}",
            disabled=not has_xlsx,
            help=None if has_xlsx else f"未找到规则库文件：{xlsx_path}",
            width="stretch",
        ):
            with st.spinner("正在从 Excel 导入规则并向量化入库..."):
                count, err = drawing_rag.import_rules_from_xlsx(project.id)
            if err:
                st.error(f"同步失败：{err}")
            else:
                st.success(f"已同步 {count} 条审图规则到知识库并完成向量化。")
                st.rerun()

    if rule_count == 0:
        st.warning("审图规则知识库为空。请点击「同步规则到知识库」，将 Excel 规则库导入并向量化，随后即可进行 RAG 图纸审核。")
    else:
        st.caption(
            f"✅ 审图规则以数据库为准（{rule_count} 条），审核时按图纸内容 RAG 检索最相关规则与 {case_count} 条人工案例。"
        )


def _status_text(status: str) -> str:
    return {
        "pending": "待分析",
        "running": "分析中",
        "done": "已分析",
        "failed": "分析失败",
        "unsupported": "不支持",
    }.get(status, "未知")


def _status_emoji(status: str) -> str:
    return {
        "pending": "⏳",
        "running": "🔄",
        "done": "✅",
        "failed": "❌",
        "unsupported": "⚠️",
    }.get(status, "•")


def _display_preview(project_id: int, doc: Document, width: int = 240):
    """显示图纸预览，PDF 显示首个转换页面."""
    file_path = _doc_file_path(project_id, doc)
    if not file_path.exists():
        st.info("文件不存在")
        return

    if file_path.suffix.lower() in IMAGE_SUFFIXES:
        st.image(str(file_path), width=width, caption="图纸预览")
        return

    if file_path.suffix.lower() == ".pdf":
        conversion_dir = Path("data") / "projects" / str(project_id) / "pdf_conversions" / str(doc.id)
        pages = sorted(conversion_dir.glob("page_*.png"))
        if pages:
            st.image(str(pages[0]), width=width, caption=f"PDF预览: {pages[0].name}")
        else:
            st.info("PDF将在开始分析时先转换为PNG预览")
        return

    st.info("该文件类型暂不支持预览")


def _drawing_sessions(project_id: int) -> list[ChatSession]:
    """获取图纸分析/审核相关聊天历史."""
    sessions = list_chat_sessions(project_id=project_id, user_id=_user_id())
    prefixes = (ANALYSIS_SESSION_PREFIX, REVIEW_SESSION_PREFIX)
    return [s for s in sessions if (s.title or "").startswith(prefixes)]


def _render_chat_images(images: list[str], collapse_threshold: int = 4):
    """渲染聊天消息中的图片，数量较多时折叠显示."""
    if not images:
        return

    container = st
    if len(images) >= collapse_threshold:
        with st.expander(f"查看本条消息关联图片（{len(images)} 张）", expanded=False):
            cols = st.columns(min(3, len(images)))
            for i, img_path in enumerate(images):
                p = Path(img_path)
                if p.exists():
                    cols[i % len(cols)].image(str(p), width="stretch")
        return

    cols = container.columns(min(3, len(images)))
    for i, img_path in enumerate(images):
        p = Path(img_path)
        if p.exists():
            cols[i % len(cols)].image(str(p), width="stretch")


def _render_message_content(content: str, max_chars: int = 1200):
    """渲染单条消息，内容过长时折叠显示更多."""
    if not content or len(content) <= max_chars:
        st.markdown(content or "")
        return

    st.markdown(content[:max_chars].rstrip() + "\n\n…")
    with st.expander("显示更多", expanded=False):
        st.markdown(content)


def _render_chat_session(session_id: int, recent_visible: int = 2):
    """渲染图纸分析会话，较早对话和长消息折叠显示."""
    messages = get_image_messages(session_id)
    split_at = max(0, len(messages) - recent_visible)

    if split_at > 0:
        with st.expander(f"查看较早对话（{split_at} 条）", expanded=False):
            for msg in messages[:split_at]:
                with st.chat_message(msg["role"]):
                    _render_chat_images(msg.get("images", []))
                    _render_message_content(msg["content"])

    for msg in messages[split_at:]:
        with st.chat_message(msg["role"]):
            _render_chat_images(msg.get("images", []))
            _render_message_content(msg["content"])


def _render_inline_chat(session_id: int, project_id: int, key_prefix: str, default_provider: str | None = None):
    """在当前图纸分析结果中展示并继续同一AI会话."""
    _render_chat_session(session_id)

    providers = get_available_providers()
    provider_default = default_provider or get_default_provider()
    provider = st.selectbox(
        "LLM 提供商",
        providers,
        index=providers.index(provider_default) if provider_default in providers else 0,
        key=f"{key_prefix}_provider",
    )

    with st.form(f"{key_prefix}_chat_form", clear_on_submit=True):
        user_input = st.text_area(
            "继续提问",
            placeholder="例如：请解释第3条提疑为什么命中，或者补充需要哪些图纸才能复核。",
            height=90,
            key=f"{key_prefix}_input",
        )
        submitted = st.form_submit_button("发送给 AI", type="primary")

    if submitted:
        if not user_input.strip():
            st.warning("请输入问题。")
            return

        previous_images = []
        for msg in get_image_messages(session_id):
            for img in msg.get("images", []):
                if img not in previous_images:
                    previous_images.append(img)

        with st.spinner("AI 正在继续分析..."):
            result = vision_chat(
                query=user_input.strip(),
                image_paths=previous_images,
                session_id=session_id,
                project_id=project_id,
                user_id=_user_id(),
                provider=provider,
            )
        if result.get("error"):
            st.error(f"错误: {result['error']}")
        else:
            st.success("已追加到当前图纸分析会话。")
            st.rerun()


def _extract_standard_review_items(markdown_text: str) -> list[dict]:
    """从审核Markdown中提取标准提疑表格行."""
    expected = ["规则编号", "部位", "问题描述", "涉及图纸", "严重程度", "建议", "可信度", "坐标"]
    rows = []
    header = None

    for raw_line in markdown_text.splitlines():
        line = raw_line.strip().replace("｜", "|")
        if not line.startswith("|") or "|" not in line[1:]:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells:
            continue
        if all(set(c.replace(" ", "")) <= {"-", ":"} for c in cells):
            continue
        if "规则编号" in cells and "问题描述" in cells:
            header = cells
            continue
        if not header:
            continue

        item = {}
        for col in expected:
            if col in header:
                idx = header.index(col)
                item[col] = cells[idx] if idx < len(cells) else ""
            else:
                item[col] = ""
        if item.get("规则编号") or item.get("问题描述"):
            rows.append(item)

    return rows


def _render_review_items_table(items: list[dict]):
    """渲染支持自动换行的审核结果表格."""
    if not items:
        return

    columns = ["规则编号", "部位", "问题描述", "涉及图纸", "严重程度", "建议", "可信度"]
    header_html = "".join(f"<th>{html.escape(col)}</th>" for col in columns)
    rows_html = []
    for item in items:
        cells = "".join(
            f"<td>{html.escape(str(item.get(col, '')))}</td>"
            for col in columns
        )
        rows_html.append(f"<tr>{cells}</tr>")

    table_html = f"""
<style>
.review-table {{
    width: 100%;
    border-collapse: collapse;
    table-layout: fixed;
    font-size: 14px;
}}
.review-table th,
.review-table td {{
    border: 1px solid #e5e7eb;
    padding: 8px 10px;
    vertical-align: top;
    white-space: normal;
    overflow-wrap: anywhere;
    word-break: break-word;
    line-height: 1.45;
}}
.review-table th {{
    background: #f3f4f6;
    font-weight: 600;
}}
.review-table th:nth-child(1), .review-table td:nth-child(1) {{ width: 9%; }}
.review-table th:nth-child(2), .review-table td:nth-child(2) {{ width: 13%; }}
.review-table th:nth-child(3), .review-table td:nth-child(3) {{ width: 25%; }}
.review-table th:nth-child(4), .review-table td:nth-child(4) {{ width: 14%; }}
.review-table th:nth-child(5), .review-table td:nth-child(5) {{ width: 8%; }}
.review-table th:nth-child(6), .review-table td:nth-child(6) {{ width: 23%; }}
.review-table th:nth-child(7), .review-table td:nth-child(7) {{ width: 8%; }}
</style>
<table class="review-table">
  <thead><tr>{header_html}</tr></thead>
  <tbody>{''.join(rows_html)}</tbody>
</table>
"""
    st.markdown(table_html, unsafe_allow_html=True)


@lru_cache(maxsize=1)
def _find_chinese_font_path() -> str | None:
    """尽量在不同系统上找到可渲染中文的字体文件."""
    font_candidates = [
        # macOS
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
        # Debian/Ubuntu with fonts-noto-cjk / fonts-wqy-zenhei
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/arphic/ukai.ttc",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
        # Common Windows fonts if mounted/copied
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simsun.ttc",
    ]
    for font_path in font_candidates:
        if Path(font_path).exists():
            return font_path

    # Linux: ask fontconfig for a font that supports Chinese.
    for query in ["Noto Sans CJK SC", "WenQuanYi Zen Hei", "Microsoft YaHei", "SimSun", "sans:lang=zh-cn"]:
        try:
            result = subprocess.run(
                ["fc-match", "-f", "%{file}", query],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
            font_path = result.stdout.strip()
            if font_path and Path(font_path).exists():
                return font_path
        except Exception:
            pass

    # Last resort: scan common font directories for likely CJK fonts.
    keywords = [
        "noto", "cjk", "wqy", "wenquanyi", "sourcehans", "sourcehan",
        "droid sans fallback", "arphic", "ukai", "uming", "simhei", "simsun", "msyh",
    ]
    for root in [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".fonts"]:
        if not root.exists():
            continue
        for font_file in root.rglob("*"):
            if font_file.suffix.lower() not in {".ttf", ".ttc", ".otf"}:
                continue
            name = font_file.name.lower()
            if any(keyword in name for keyword in keywords):
                return str(font_file)

    return None


def _load_annotation_font(size: int, bold: bool = False):
    """加载支持中文的标注字体，失败时回退到Pillow默认字体."""
    font_path = _find_chinese_font_path()
    if font_path:
        try:
            return ImageFont.truetype(font_path, size=size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _has_chinese_annotation_font() -> bool:
    """判断当前环境是否找到了中文字体."""
    return _find_chinese_font_path() is not None
def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    """兼容不同 Pillow 版本获取文本宽高."""
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]
    except Exception:
        return draw.textsize(text, font=font)


def _wrap_text_for_draw(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    """按绘制宽度为中英文混排文本换行，并保留显式换行符."""
    text = str(text or "")
    if not text.strip():
        return [""]

    lines = []
    for segment in text.split("\n"):
        segment = segment.strip()
        if not segment:
            lines.append("")
            continue
        current = ""
        for char in segment:
            trial = current + char
            width, _ = _text_size(draw, trial, font)
            if current and width > max_width:
                lines.append(current)
                current = char
            else:
                current = trial
        if current:
            lines.append(current)
    return lines


def _review_severity_color(severity: str) -> tuple[int, int, int]:
    """根据严重程度返回图例颜色."""
    text = str(severity or "").strip()
    if "高" in text or "严重" in text:
        return (220, 38, 38)
    if "中" in text:
        return (245, 158, 11)
    if "低" in text:
        return (37, 99, 235)
    return (107, 114, 128)


def _annotated_image_output_path(project_id: int, doc_id: int, image_path: Path, page_index: int) -> Path:
    """返回自动图例标注图的输出路径."""
    output_dir = Path("data") / "projects" / str(project_id) / "annotated_drawings" / str(doc_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_stem = re.sub(r"[^\w\-.一-鿿]+", "_", image_path.stem).strip("_") or f"page_{page_index + 1:03d}"
    return output_dir / f"{safe_stem}_审核标注_{page_index + 1:03d}.png"


def _parse_review_item_bbox(item: dict) -> tuple[float, float, float, float] | None:
    """解析提疑“坐标”字段中的归一化 bbox（0-1000），返回 (x1,y1,x2,y2) 比例值 [0,1]。

    模型输出格式约定为 "x1,y1,x2,y2"（0-1000）。无法解析或明确为“无”时返回 None。
    """
    raw = str(item.get("坐标", "") or "").strip()
    if not raw or raw in {"无", "-", "—", "N/A", "n/a", "null", "None"}:
        return None

    nums = re.findall(r"-?\d+(?:\.\d+)?", raw)
    if len(nums) < 4:
        return None

    try:
        # 取末尾 4 个数，避免误取到“第N页”等前缀数字
        x1, y1, x2, y2 = (float(n) for n in nums[-4:])
    except ValueError:
        return None

    # 归一化到 [0,1]；若数值看起来是 0-1 之间的小数则直接用，否则按 0-1000 处理
    scale = 1.0 if max(x1, y1, x2, y2) <= 1.0 else 1000.0
    x1, y1, x2, y2 = x1 / scale, y1 / scale, x2 / scale, y2 / scale

    # 规整顺序并裁剪到 [0,1]
    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))
    x1, y1, x2, y2 = (max(0.0, min(1.0, v)) for v in (x1, y1, x2, y2))

    if x2 - x1 < 1e-3 or y2 - y1 < 1e-3:
        return None
    return x1, y1, x2, y2


def _normalize_manual_bbox(bbox) -> tuple[float, float, float, float] | None:
    """把 (x1,y1,x2,y2) 归一化到 [0,1]，排除越界/退化的框；缺失时返回 None。"""
    if bbox is None:
        return None
    try:
        x1, y1, x2, y2 = (float(v) for v in list(bbox)[:4])
    except (TypeError, ValueError):
        return None
    # 若显然按像素/千分位输入（最大值明显 > 1.5），做一次尺度归一
    m = max(abs(x1), abs(y1), abs(x2), abs(y2))
    if m > 1.5:
        scale = 1000.0 if m <= 1000.0 else m
        x1, y1, x2, y2 = x1 / scale, y1 / scale, x2 / scale, y2 / scale
    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))
    x1, y1, x2, y2 = (max(0.0, min(1.0, v)) for v in (x1, y1, x2, y2))
    if x2 - x1 < 1e-3 or y2 - y1 < 1e-3:
        return None
    return x1, y1, x2, y2


def _build_review_marker_image(image_path: Path, page_items: list[dict], output_path: Path) -> tuple[Path, int]:
    """在原图上按“坐标”字段绘制编号方框/圆点，编号与右侧系统审核列表一致。

    返回 (输出图路径, 已标注问题数)。无任何可定位坐标时仍复制原图，标注数为 0。
    """
    with Image.open(image_path) as source:
        base_img = source.convert("RGB")

    width, height = base_img.size
    draw = ImageDraw.Draw(base_img, "RGBA")
    line_w = max(2, round(min(width, height) / 400))
    badge_r = max(12, round(min(width, height) / 90))
    badge_font = _load_annotation_font(max(16, round(min(width, height) / 70)))

    marked = 0
    for idx, item in enumerate(page_items, start=1):
        bbox = _parse_review_item_bbox(item)
        if not bbox:
            continue
        marked += 1
        x1, y1, x2, y2 = bbox
        px1, py1, px2, py2 = x1 * width, y1 * height, x2 * width, y2 * height
        color = _review_severity_color(item.get("严重程度", ""))

        # 半透明填充 + 实线边框
        draw.rectangle([px1, py1, px2, py2], fill=color + (46,), outline=color, width=line_w)

        # 左上角编号徽标
        bx, by = px1, py1
        draw.ellipse(
            [bx - badge_r, by - badge_r, bx + badge_r, by + badge_r],
            fill=color, outline=(255, 255, 255), width=max(1, line_w // 2),
        )
        num = str(idx)
        num_w, num_h = _text_size(draw, num, badge_font)
        draw.text((bx - num_w / 2, by - num_h / 2 - 1), num, fill="white", font=badge_font)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    base_img.save(output_path, format="PNG")
    return output_path, marked


def _review_item_text_for_page_match(item: dict) -> str:
    """拼接可用于识别页码的审核提疑文本."""
    return " ".join(
        str(item.get(col, ""))
        for col in ["部位", "涉及图纸", "问题描述"]
        if item.get(col)
    )


def _extract_review_item_pages(item: dict, total_pages: int) -> set[int]:
    """从审核提疑文本中提取 1-based 页码集合."""
    text = _review_item_text_for_page_match(item)
    if not text:
        return set()

    pages: set[int] = set()
    patterns = [
        r"第\s*(\d{1,3})\s*页",
        r"page\s*(\d{1,3})\b",
        r"p\.?\s*(\d{1,3})\b",
        r"页码\s*[:：]?\s*(\d{1,3})\b",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            page = int(match.group(1))
            if 1 <= page <= total_pages:
                pages.add(page)

    # 兼容转换图文件名或模型引用的 page_001 / page-001 / page 001
    for match in re.finditer(r"page[_\-\s]*(\d{1,3})\b", text, flags=re.IGNORECASE):
        page = int(match.group(1))
        if 1 <= page <= total_pages:
            pages.add(page)

    return pages


def _split_review_items_for_page(review_items: list[dict], page_number: int, total_pages: int) -> tuple[list[dict], list[dict]]:
    """按页码拆分当前页提疑和未识别页码提疑."""
    if total_pages <= 1:
        return review_items, []

    current_page_items = []
    unknown_page_items = []
    for item in review_items:
        pages = _extract_review_item_pages(item, total_pages)
        if page_number in pages:
            current_page_items.append(item)
        elif not pages:
            unknown_page_items.append(item)
    return current_page_items, unknown_page_items


def _review_page_match_summary(review_items: list[dict], total_pages: int) -> dict[int, int]:
    """统计每页可识别到的审核提疑数量."""
    summary = {page: 0 for page in range(1, total_pages + 1)}
    for item in review_items:
        for page in _extract_review_item_pages(item, total_pages):
            summary[page] += 1
    return summary


def _load_manual_comments(analysis_data: dict) -> dict:
    """从分析数据取出人工批注（按页码组织）."""
    comments = analysis_data.get("manual_comments")
    return comments if isinstance(comments, dict) else {}


def _get_page_manual_comments(analysis_data: dict, page_number: int) -> list[dict]:
    """取当前页人工批注."""
    comments = _load_manual_comments(analysis_data)
    page_comments = comments.get(f"page_{page_number}")
    return page_comments if isinstance(page_comments, list) else []


def _current_user_label() -> str:
    """获取当前用户展示名称."""
    user = st.session_state.get("user") or {}
    return user.get("name") or user.get("email") or "匿名用户"


def _save_manual_comments(doc_id: int, comments: dict):
    """仅更新 analysis_data_json 中的 manual_comments，保留其他分析字段."""
    with session() as s:
        doc_db = s.query(Document).filter(Document.id == doc_id).first()
        if not doc_db:
            return
        data = {}
        if doc_db.analysis_data_json:
            try:
                data = json.loads(doc_db.analysis_data_json)
            except json.JSONDecodeError:
                data = {}
        data["manual_comments"] = comments
        doc_db.analysis_data_json = json.dumps(data, ensure_ascii=False, indent=2)
        s.commit()


def _comment_display_parts(comment: dict) -> tuple[str, str]:
    """从批注 dict 里读出 (问题描述, 建议方案)。

    新批注两字段独立存储；旧批注只有 text 时，返回 (text, '')，保证向后兼容。
    """
    problem = str(comment.get("problem", "") or "").strip()
    suggestion = str(comment.get("suggestion", "") or "").strip()
    if not problem and not suggestion:
        legacy = str(comment.get("text", "") or "").strip()
        return legacy, ""
    return problem, suggestion


def _compose_comment_text(problem: str, suggestion: str) -> str:
    """把问题描述+建议方案拼成一段易读文本，用于向量化/单行渲染/导出兜底。"""
    problem = (problem or "").strip()
    suggestion = (suggestion or "").strip()
    if problem and suggestion:
        return f"问题：{problem}\n建议：{suggestion}"
    if problem:
        return problem
    return suggestion


def _add_manual_comment(doc_id: int, page_number: int, text: str,
                        verdict: str = "manual", rule_code: str | None = None,
                        location: str | None = None, problem: str | None = None,
                        suggestion: str | None = None, severity: str | None = None,
                        bbox: tuple[float, float, float, float] | list[float] | None = None):
    """新增一条当前页人工批注，并自动向量化入人工案例知识库.

    参数说明:
      - text: 兼容旧调用；未拆分成 problem/suggestion 时的整合文本。
      - problem / suggestion: 分开的“问题描述”和“建议方案”，两者至少一个非空即算有效。
      - bbox: 可选归一化 (x1,y1,x2,y2)（[0,1]，左上原点），来自图上拖拽画框。
    """
    # 允许两种入参方式：拆分 problem/suggestion 优先；否则用 text
    problem = (problem or "").strip()
    suggestion = (suggestion or "").strip()
    text = (text or "").strip()
    composed = _compose_comment_text(problem, suggestion) if (problem or suggestion) else text
    if not composed:
        return

    project_id = None
    with session() as s:
        doc_db = s.query(Document).filter(Document.id == doc_id).first()
        if not doc_db:
            return
        project_id = doc_db.project_id
        data = {}
        if doc_db.analysis_data_json:
            try:
                data = json.loads(doc_db.analysis_data_json)
            except json.JSONDecodeError:
                data = {}
        comments = data.get("manual_comments")
        if not isinstance(comments, dict):
            comments = {}
        page_key = f"page_{page_number}"
        page_comments = comments.get(page_key)
        if not isinstance(page_comments, list):
            page_comments = []
        stamp = now_utc().isoformat()
        comment_row = {
            "id": f"c_{uuid.uuid4().hex[:8]}",
            "problem": problem,
            "suggestion": suggestion,
            "text": composed,  # 兼容旧代码/导出兜底
            "author": _current_user_label(),
            "created_at": stamp,
            "updated_at": stamp,
        }
        # 归一化 bbox 后写入（越界或退化的框直接丢弃，避免脏数据）
        norm_bbox = _normalize_manual_bbox(bbox)
        if norm_bbox is not None:
            comment_row["bbox"] = list(norm_bbox)
        page_comments.append(comment_row)
        comments[page_key] = page_comments
        data["manual_comments"] = comments
        doc_db.analysis_data_json = json.dumps(data, ensure_ascii=False, indent=2)
        s.commit()

    # 人工反馈自动向量化入库，参与后续审核 RAG 检索
    if project_id is not None:
        try:
            drawing_rag.add_review_case(
                project_id=project_id,
                text=composed,
                source_document_id=doc_id,
                page_num=page_number,
                rule_code=rule_code,
                location=location,
                problem=problem or (None if rule_code else composed),
                suggestion=suggestion or None,
                severity=severity,
                verdict=verdict,
                author=_current_user_label(),
            )
        except Exception:
            # 向量化失败不应阻断批注保存
            pass


def _update_manual_comment(doc_id: int, page_number: int, comment_id: str,
                           problem: str | None = None, suggestion: str | None = None,
                           text: str | None = None):
    """编辑一条当前页人工批注。

    - 提供 problem/suggestion 时按新格式写；两者至少一个非空
    - 只提供 text 时按旧格式写（向后兼容）
    """
    problem = (problem or "").strip() if problem is not None else None
    suggestion = (suggestion or "").strip() if suggestion is not None else None
    legacy_text = (text or "").strip() if text is not None else None

    # 至少要有一个非空内容
    if not any([problem, suggestion, legacy_text]):
        return

    with session() as s:
        doc_db = s.query(Document).filter(Document.id == doc_id).first()
        if not doc_db or not doc_db.analysis_data_json:
            return
        try:
            data = json.loads(doc_db.analysis_data_json)
        except json.JSONDecodeError:
            return
        comments = data.get("manual_comments")
        if not isinstance(comments, dict):
            return
        page_comments = comments.get(f"page_{page_number}")
        if not isinstance(page_comments, list):
            return
        for comment in page_comments:
            if comment.get("id") == comment_id:
                if problem is not None or suggestion is not None:
                    comment["problem"] = problem or ""
                    comment["suggestion"] = suggestion or ""
                    comment["text"] = _compose_comment_text(problem or "", suggestion or "")
                elif legacy_text is not None:
                    comment["text"] = legacy_text
                    # 旧格式编辑时清空拆分字段，保持一致
                    comment.pop("problem", None)
                    comment.pop("suggestion", None)
                comment["updated_at"] = now_utc().isoformat()
                break
        data["manual_comments"] = comments
        doc_db.analysis_data_json = json.dumps(data, ensure_ascii=False, indent=2)
        s.commit()


def _delete_manual_comment(doc_id: int, page_number: int, comment_id: str):
    """删除一条当前页人工批注."""
    with session() as s:
        doc_db = s.query(Document).filter(Document.id == doc_id).first()
        if not doc_db or not doc_db.analysis_data_json:
            return
        try:
            data = json.loads(doc_db.analysis_data_json)
        except json.JSONDecodeError:
            return
        comments = data.get("manual_comments")
        if not isinstance(comments, dict):
            return
        page_key = f"page_{page_number}"
        page_comments = comments.get(page_key)
        if not isinstance(page_comments, list):
            return
        comments[page_key] = [c for c in page_comments if c.get("id") != comment_id]
        data["manual_comments"] = comments
        doc_db.analysis_data_json = json.dumps(data, ensure_ascii=False, indent=2)
        s.commit()


def _build_review_legend_image(image_path: Path, review_items: list[dict], output_path: Path, page_number: int | None = None, manual_comments: list[dict] | None = None) -> Path:
    """在图纸右侧扩展当前页审核问题编号图例，并保存为PNG."""
    with Image.open(image_path) as source:
        base_img = source.convert("RGB")

    width, height = base_img.size
    sidebar_width = max(420, min(720, max(width // 2, 1)))
    padding = 24
    item_gap = 18
    title_font = _load_annotation_font(max(24, min(40, width // 45)))
    body_font = _load_annotation_font(max(18, min(28, width // 70)))
    small_font = _load_annotation_font(max(15, min(22, width // 90)))

    annotated = Image.new("RGB", (width + sidebar_width, height), "white")
    annotated.paste(base_img, (0, 0))
    draw = ImageDraw.Draw(annotated)

    # 分隔线与标题
    draw.rectangle([width, 0, width + 2, height], fill=(229, 231, 235))
    title = f"第 {page_number} 页审核问题" if page_number else "审核问题图例"
    draw.text((width + padding, padding), title, fill=(17, 24, 39), font=title_font)
    _, title_h = _text_size(draw, title, title_font)
    y = padding + title_h + 22

    if not review_items:
        note = "本页暂无可匹配到页码的审核问题。"
        for line in _wrap_text_for_draw(draw, note, body_font, sidebar_width - padding * 2):
            draw.text((width + padding, y), line, fill=(107, 114, 128), font=body_font)
            _, line_h = _text_size(draw, line, body_font)
            y += line_h + 8
        _draw_manual_comments_section(
            draw, manual_comments, width, sidebar_width, height, padding, y,
            title_font, body_font, small_font,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        annotated.save(output_path, format="PNG")
        return output_path

    max_text_width = sidebar_width - padding * 2 - 52
    visible_count = 0
    for idx, item in enumerate(review_items, start=1):
        severity = item.get("严重程度", "") or "待复核"
        color = _review_severity_color(severity)
        rule_id = item.get("规则编号", "") or "未编号"
        location = item.get("部位", "") or "部位未明确"
        description = item.get("问题描述", "") or "问题描述为空"
        header = f"{idx}. [{rule_id}] {severity}"
        detail = f"部位：{location}；问题：{description}"

        header_lines = _wrap_text_for_draw(draw, header, body_font, max_text_width)
        detail_lines = _wrap_text_for_draw(draw, detail, small_font, max_text_width)
        _, body_h = _text_size(draw, "国", body_font)
        _, small_h = _text_size(draw, "国", small_font)
        item_height = max(34, len(header_lines) * (body_h + 4) + min(3, len(detail_lines)) * (small_h + 4)) + item_gap

        if y + item_height > height - padding:
            remaining = len(review_items) - visible_count
            if remaining > 0:
                note = f"本页其余 {remaining} 条请查看下方审核表格。"
                note_lines = _wrap_text_for_draw(draw, note, small_font, sidebar_width - padding * 2)
                note_y = max(y, height - padding - len(note_lines) * (small_h + 4) - 8)
                for line in note_lines:
                    draw.text((width + padding, note_y), line, fill=(107, 114, 128), font=small_font)
                    note_y += small_h + 4
            break

        circle_x = width + padding + 16
        circle_y = y + 15
        draw.ellipse([circle_x - 15, circle_y - 15, circle_x + 15, circle_y + 15], fill=color, outline=(31, 41, 55), width=2)
        number_text = str(idx)
        num_w, num_h = _text_size(draw, number_text, small_font)
        draw.text((circle_x - num_w / 2, circle_y - num_h / 2 - 1), number_text, fill="white", font=small_font)

        text_x = width + padding + 44
        text_y = y
        for line in header_lines:
            draw.text((text_x, text_y), line, fill=(17, 24, 39), font=body_font)
            text_y += body_h + 4
        for line in detail_lines[:3]:
            draw.text((text_x, text_y), line, fill=(55, 65, 81), font=small_font)
            text_y += small_h + 4
        if len(detail_lines) > 3:
            draw.text((text_x, text_y), "…", fill=(107, 114, 128), font=small_font)

        y += item_height
        visible_count += 1

    _draw_manual_comments_section(
        draw, manual_comments, width, sidebar_width, height, padding, y,
        title_font, body_font, small_font,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    annotated.save(output_path, format="PNG")
    return output_path


def _draw_manual_comments_section(draw, manual_comments, width, sidebar_width, height, padding, y, title_font, body_font, small_font):
    """在右侧图例栏底部绘制人工批注分区."""
    comments = manual_comments or []
    if not comments:
        return

    _, body_h = _text_size(draw, "国", body_font)
    _, small_h = _text_size(draw, "国", small_font)
    max_text_width = sidebar_width - padding * 2

    # 与上方内容留出间隔，并画分隔线
    y += 14
    if y > height - padding:
        return
    draw.rectangle([width + padding, y, width + sidebar_width - padding, y + 1], fill=(209, 213, 219))
    y += 14

    heading = "人工批注"
    draw.text((width + padding, y), heading, fill=(17, 24, 39), font=title_font)
    _, heading_h = _text_size(draw, heading, title_font)
    y += heading_h + 12

    for idx, comment in enumerate(comments, start=1):
        author = comment.get("author", "") or "匿名用户"
        problem_val, suggestion_val = _comment_display_parts(comment)
        created_at = comment.get("created_at", "")
        time_str = ""
        if created_at:
            try:
                time_str = format_beijing(datetime.fromisoformat(created_at), "%Y-%m-%d %H:%M")
            except Exception:
                time_str = ""

        header = f"{idx}. {author}" + (f" · {time_str}" if time_str else "")
        header_lines = _wrap_text_for_draw(draw, header, small_font, max_text_width)

        # 问题描述 / 建议方案 两段独立渲染；缺失字段用兜底
        body_segments: list[tuple[str, tuple[int, int, int]]] = []
        if problem_val:
            body_segments.append((f"问题：{problem_val}", (31, 41, 55)))
        if suggestion_val:
            body_segments.append((f"建议：{suggestion_val}", (55, 65, 81)))
        if not body_segments:
            legacy = str(comment.get("text", "") or "")
            if legacy:
                body_segments.append((legacy, (31, 41, 55)))

        wrapped_body: list[tuple[list[str], tuple[int, int, int]]] = [
            (_wrap_text_for_draw(draw, seg, body_font, max_text_width), color)
            for seg, color in body_segments
        ]
        body_line_count = sum(len(lines) for lines, _ in wrapped_body)
        block_height = len(header_lines) * (small_h + 3) + body_line_count * (body_h + 4) + 12

        if y + block_height > height - padding:
            remaining = len(comments) - idx + 1
            note = f"其余 {remaining} 条人工批注请在页面下方查看。"
            note_y = max(y, height - padding - (small_h + 4))
            for line in _wrap_text_for_draw(draw, note, small_font, max_text_width):
                draw.text((width + padding, note_y), line, fill=(107, 114, 128), font=small_font)
                note_y += small_h + 4
            break

        for line in header_lines:
            draw.text((width + padding, y), line, fill=(37, 99, 235), font=small_font)
            y += small_h + 3
        for body_lines, color in wrapped_body:
            for line in body_lines:
                draw.text((width + padding, y), line, fill=color, font=body_font)
                y += body_h + 4
        y += 10


def _build_comment_stitched_image(image_path: Path, page_number: int, comments: list[dict]) -> Image.Image:
    """将单页图纸与该页人工批注拼接为一张图（右侧批注栏，画布高度自适应）."""
    with Image.open(image_path) as source:
        base_img = source.convert("RGB")

    width, height = base_img.size
    sidebar_width = max(420, min(760, max(width // 2, 1)))
    padding = 24
    title_font = _load_annotation_font(max(24, min(40, width // 45)))
    body_font = _load_annotation_font(max(18, min(28, width // 70)))
    small_font = _load_annotation_font(max(15, min(22, width // 90)))

    # 用实际字高计算行距（与 _build_review_legend_image 一致），避免文字重叠
    measure = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    _, body_ch = _text_size(measure, "国", body_font)
    _, small_ch = _text_size(measure, "国", small_font)
    _, title_ch = _text_size(measure, "国", title_font)
    body_h = body_ch + 8
    small_h = small_ch + 6
    title_h = title_ch + 10
    comment_gap = 16
    max_text_width = sidebar_width - padding * 2

    def _segments_of(comment: dict) -> list[tuple[str, tuple[int, int, int]]]:
        problem_val, suggestion_val = _comment_display_parts(comment)
        segs: list[tuple[str, tuple[int, int, int]]] = []
        if problem_val:
            segs.append((f"问题：{problem_val}", (31, 41, 55)))
        if suggestion_val:
            segs.append((f"建议：{suggestion_val}", (55, 65, 81)))
        if not segs:
            legacy = str(comment.get("text", "") or "")
            if legacy:
                segs.append((legacy, (31, 41, 55)))
        return segs

    needed = padding + title_h + 18
    for idx, comment in enumerate(comments, start=1):
        author = comment.get("author", "") or "匿名用户"
        created_at = comment.get("created_at", "")
        time_str = ""
        if created_at:
            try:
                time_str = format_beijing(datetime.fromisoformat(created_at), "%Y-%m-%d %H:%M")
            except Exception:
                time_str = ""
        header = f"{idx}. {author}" + (f" · {time_str}" if time_str else "")
        header_lines = _wrap_text_for_draw(measure, header, small_font, max_text_width)
        body_line_count = 0
        for seg, _ in _segments_of(comment):
            body_line_count += len(_wrap_text_for_draw(measure, seg, body_font, max_text_width))
        needed += len(header_lines) * small_h + body_line_count * body_h + comment_gap
    needed += padding

    canvas_height = max(height, needed)
    annotated = Image.new("RGB", (width + sidebar_width, canvas_height), "white")
    annotated.paste(base_img, (0, 0))
    draw = ImageDraw.Draw(annotated)
    draw.rectangle([width, 0, width + 2, canvas_height], fill=(229, 231, 235))

    y = padding
    title = f"第 {page_number} 页人工批注"
    draw.text((width + padding, y), title, fill=(17, 24, 39), font=title_font)
    y += title_h + 18

    if not comments:
        for line in _wrap_text_for_draw(draw, "本页暂无人工批注。", body_font, max_text_width):
            draw.text((width + padding, y), line, fill=(107, 114, 128), font=body_font)
            y += body_h
        return annotated

    for idx, comment in enumerate(comments, start=1):
        author = comment.get("author", "") or "匿名用户"
        created_at = comment.get("created_at", "")
        time_str = ""
        if created_at:
            try:
                time_str = format_beijing(datetime.fromisoformat(created_at), "%Y-%m-%d %H:%M")
            except Exception:
                time_str = ""
        header = f"{idx}. {author}" + (f" · {time_str}" if time_str else "")
        for line in _wrap_text_for_draw(draw, header, small_font, max_text_width):
            draw.text((width + padding, y), line, fill=(37, 99, 235), font=small_font)
            y += small_h
        for seg, color in _segments_of(comment):
            for line in _wrap_text_for_draw(draw, seg, body_font, max_text_width):
                draw.text((width + padding, y), line, fill=color, font=body_font)
                y += body_h
        y += comment_gap

    return annotated


def _export_comment_annotated_pdf(doc: Document, analysis_data: dict) -> bytes | None:
    """将每页图纸与人工批注拼接后合成一个多页PDF；无可用图片时返回None."""
    image_paths = [Path(p) for p in analysis_data.get("preprocessed_images", []) if Path(p).exists()]
    if not image_paths:
        return None

    pages = []
    for idx, image_path in enumerate(image_paths):
        page_number = idx + 1
        comments = _get_page_manual_comments(analysis_data, page_number)
        try:
            pages.append(_build_comment_stitched_image(image_path, page_number, comments))
        except Exception:
            continue

    if not pages:
        return None

    from io import BytesIO
    buffer = BytesIO()
    pages[0].save(buffer, format="PDF", save_all=True, append_images=pages[1:])
    return buffer.getvalue()


def _render_review_legend_images(doc: Document, review_items: list[dict], analysis_data: dict):
    """渲染当前页审核结果自动图例标注图，并提供PNG下载."""
    image_paths = []
    for raw_path in analysis_data.get("preprocessed_images", []):
        path = Path(raw_path)
        if path.exists():
            image_paths.append(path)

    if not image_paths:
        st.info("暂无可生成标注图的预处理图片。请重新执行图纸审核以生成图片预处理结果。")
        return

    total_pages = len(image_paths)
    page_summary = _review_page_match_summary(review_items, total_pages)
    page_labels = _load_page_labels(analysis_data)

    def _page_display(idx: int) -> str:
        n = idx + 1
        label = _format_page_label(page_labels, n)
        base = f"第 {n} 页"
        if label:
            base = f"{base}{label}"
        return f"{base} · 已匹配 {page_summary.get(n, 0)} 条 · {image_paths[idx].name}"

    selected_index = 0
    if total_pages > 1:
        page_key = f"review_legend_page_{doc.id}"
        current = st.session_state.get(page_key, 0)
        if not isinstance(current, int) or not (0 <= current < total_pages):
            current = 0

        col_prev, col_select, col_next = st.columns([1, 4, 1])
        with col_prev:
            if st.button(
                "⬅️ 上一页",
                key=f"review_legend_prev_{doc.id}",
                disabled=current <= 0,
                width="stretch",
            ):
                st.session_state[page_key] = current - 1
                st.rerun()
        with col_next:
            if st.button(
                "下一页 ➡️",
                key=f"review_legend_next_{doc.id}",
                disabled=current >= total_pages - 1,
                width="stretch",
            ):
                st.session_state[page_key] = current + 1
                st.rerun()
        with col_select:
            selected_index = st.selectbox(
                "选择标注图页码",
                range(total_pages),
                format_func=_page_display,
                index=current,
                key=page_key,
            )

    page_number = int(selected_index) + 1
    page_label_suffix = _format_page_label(page_labels, page_number)
    current_page_items, unknown_page_items = _split_review_items_for_page(review_items, page_number, total_pages)
    if total_pages > 1:
        page_display = f"第 {page_number} 页"
        if page_label_suffix:
            page_display = f"第 {page_number} 页 {page_label_suffix}"
        st.caption(
            f"当前{page_display}：显示 {len(current_page_items)} 条已明确匹配到本页的审核问题；"
            f"{len(unknown_page_items)} 条未识别页码的问题未标到具体页面。"
        )
        if unknown_page_items:
            with st.expander(f"查看未识别页码的问题（{len(unknown_page_items)} 条）", expanded=False):
                _render_review_items_table(unknown_page_items)

    current_image = image_paths[selected_index]
    page_comments = _get_page_manual_comments(analysis_data, page_number)

    # 在原图上按 AI 估算坐标绘制编号标记（编号与右侧系统审核列表一致）
    display_image = current_image
    marked_count = 0
    if any(_parse_review_item_bbox(it) for it in current_page_items):
        marker_path = _annotated_image_output_path(doc.project_id, doc.id, current_image, selected_index)
        try:
            display_image, marked_count = _build_review_marker_image(
                current_image, current_page_items, marker_path
            )
        except Exception:
            display_image, marked_count = current_image, 0

    col_image, col_review, col_comments = st.columns([6, 2, 2])

    # 读取“在图上放大”所选问题，计算需要居中放大的归一化坐标框
    focus_key = f"review_focus_bbox_{doc.id}_{page_number}"
    focus_idx = st.session_state.get(focus_key)
    focus_bbox = None
    if isinstance(focus_idx, int) and 1 <= focus_idx <= len(current_page_items):
        focus_bbox = _parse_review_item_bbox(current_page_items[focus_idx - 1])
    if not focus_bbox:
        # 所选问题无坐标或状态失效时清除，避免残留
        focus_idx = None

    # 检查是否有人工批注需要定位
    manual_focus_key = f"manual_focus_{doc.id}_{page_number}"
    manual_focus_comment_id = st.session_state.get(manual_focus_key)
    manual_focus_bbox = None
    if manual_focus_comment_id is not None:
        for comment in page_comments:
            if comment.get("id") == manual_focus_comment_id:
                bbox = comment.get("bbox")
                if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
                    manual_focus_bbox = tuple(float(v) for v in bbox[:4])
                    break
        if not manual_focus_bbox:
            # 找不到批注或bbox无效时清除
            st.session_state.pop(manual_focus_key, None)
            manual_focus_comment_id = None

    # 最终用于定位的 bbox：优先人工批注，其次系统审核问题
    final_focus_bbox = manual_focus_bbox or focus_bbox

    # 人工批注位置选择：存储当前页选定的 bbox 到 session_state
    manual_bbox_key = f"manual_bbox_{doc.id}_{page_number}"
    manual_bbox_norm = st.session_state.get(manual_bbox_key, None)

    with col_image:
        if marked_count:
            st.caption(f"已在图上标注 {marked_count} 处问题位置（编号与右侧系统审核一致，颜色对应严重程度；坐标由 AI 估算，仅供参考）。")
        else:
            st.caption("本页问题暂无可用于图上定位的坐标，显示原始页图。")
        if manual_focus_bbox:
            st.caption(f"🔍 已定位到人工批注位置，图片自动放大居中；点查看器内“复位”可看整页。")
        elif focus_bbox:
            st.caption(f"🔍 已定位到问题 {focus_idx}，图片自动放大居中；点查看器内“复位”可看整页。")

        # 标注模式状态
        annotate_mode_key = f"annotate_mode_{doc.id}_{page_number}"
        annotate_mode = st.session_state.get(annotate_mode_key, False)

        # 标注模式下的退出按钮
        if CANVAS_AVAILABLE and annotate_mode:
            if st.button("❌ 退出标注", key=f"cancel_annotate_{doc.id}_{page_number}"):
                st.session_state[annotate_mode_key] = False
                st.rerun()
        elif not CANVAS_AVAILABLE:
            st.caption("❌ `streamlit-drawable-canvas` 未安装，请先执行：`pip install streamlit-drawable-canvas`")

        # key 随所选问题变化，确保 iframe 重新挂载并执行定位脚本
        focus_suffix = f"{focus_idx or 0}_{manual_focus_comment_id or ''}"
        viewer_key = f"review_legend_image_{doc.id}_{selected_index}_focus{focus_suffix}"
        caption_title = f"第 {page_number} 页图纸"
        if page_label_suffix:
            caption_title = f"第 {page_number} 页 {page_label_suffix}"
        _render_full_resolution_image(
            display_image,
            caption_title,
            key=viewer_key,
            focus_bbox=final_focus_bbox,
        )
        st.download_button(
            label="🖼️ 下载当前页标注图 PNG" if marked_count else "🖼️ 下载当前页图纸 PNG",
            data=display_image.read_bytes(),
            file_name=f"{Path(doc.filename).stem}_第{page_number:03d}页.png",
            mime="image/png",
            key=f"download_review_legend_{doc.id}_{selected_index}",
        )

        # 如果在标注模式，才在下方显示标注画布
        if CANVAS_AVAILABLE and annotate_mode:
            st.divider()
            st.subheader("✏️ 标注问题位置")
            # 使用更大的画布尺寸适配左侧宽栏
            resized_bg = None
            canvas_w, canvas_h = 0, 0
            try:
                if not current_image.exists():
                    st.error("⚠️ 图片文件不存在，无法进行位置标注。")
                else:
                    with Image.open(current_image) as bg:
                        bg = bg.convert("RGB")
                        orig_w, orig_h = bg.size
                        # 左侧比较宽，使用更大的 max_width
                        canvas_w, canvas_h = _canvas_display_size(orig_w, orig_h, max_width=720, max_height=720)
                        # 提前 resize，保持图片在 with 块内处理
                        resized_bg = bg.resize((canvas_w, canvas_h))
            except Exception as e:
                st.error(f"加载图片失败：{str(e)}")
                canvas_w, canvas_h = 0, 0

            if canvas_w > 0 and resized_bg is not None:
                # 把当前页已有的人工批注框作为绿色只读矩形叠在背景上
                existing_rects = []
                for c in page_comments:
                    b = c.get("bbox")
                    if isinstance(b, (list, tuple)) and len(b) == 4:
                        existing_rects.append(_bbox_to_canvas_rect(b, canvas_w, canvas_h, stroke_color="#22c55e"))

                st.caption("在下方图上按住鼠标**拖拽画一个矩形**来框选问题位置（已有的绿色框为历史批注）。画好后位置会自动保存，到右侧填写批注内容即可。")
                canvas_result = st_canvas(
                    fill_color="rgba(239, 68, 68, 0.12)",
                    stroke_width=2,
                    stroke_color="#ef4444",
                    background_image=resized_bg,
                    update_streamlit=True,
                    height=canvas_h,
                    width=canvas_w,
                    drawing_mode="rect",
                    display_toolbar=True,
                    initial_drawing={"version": "4.4.0", "objects": existing_rects} if existing_rects else None,
                    key=f"canvas_{doc.id}_{page_number}_large",
                )

                # 取最后一个由用户新画的矩形，保存到 session_state
                if canvas_result is not None and canvas_result.json_data is not None:
                    new_rects = [
                        obj for obj in canvas_result.json_data.get("objects", [])
                        if obj.get("type") == "rect" and obj.get("selectable", True)
                    ]
                    if new_rects:
                        last = new_rects[-1]
                        left = float(last.get("left", 0))
                        top = float(last.get("top", 0))
                        w = float(last.get("width", 0)) * float(last.get("scaleX", 1) or 1)
                        h = float(last.get("height", 0)) * float(last.get("scaleY", 1) or 1)
                        if w > 0 and h > 0:
                            x1 = left / canvas_w
                            y1 = top / canvas_h
                            x2 = (left + w) / canvas_w
                            y2 = (top + h) / canvas_h
                            manual_bbox_norm = _normalize_manual_bbox((x1, y1, x2, y2))
                            st.session_state[manual_bbox_key] = manual_bbox_norm

                # 显示当前已选定位置
                if manual_bbox_norm:
                    st.caption(
                        f"✅ 已选定标注位置：({manual_bbox_norm[0]*100:.0f}%, {manual_bbox_norm[1]*100:.0f}%) → "
                        f"({manual_bbox_norm[2]*100:.0f}%, {manual_bbox_norm[3]*100:.0f}%)"
                    )

                # 取消和清除按钮
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("❌ 取消标注", key=f"cancel_annotate_{doc.id}_{page_number}", use_container_width=True):
                        st.session_state[annotate_mode_key] = False
                        st.rerun()
                with col2:
                    if manual_bbox_norm:
                        if st.button("🗑️ 清除位置", key=f"clear_bbox_{doc.id}_{page_number}", use_container_width=True):
                            st.session_state.pop(manual_bbox_key, None)
                            manual_bbox_norm = None
                            st.rerun()
            elif annotate_mode:
                # 加载失败，显示取消按钮
                if st.button("❌ 取消标注", key=f"cancel_annotate_fail_{doc.id}_{page_number}", use_container_width=True):
                    st.session_state[annotate_mode_key] = False
                    st.rerun()

    with col_review:
        _render_page_review_items(doc, page_number, current_page_items,
                                  page_label_suffix=page_label_suffix)

    with col_comments:
        _render_manual_comments_ui(doc, page_number, page_comments,
                                   page_image_path=current_image,
                                   page_label_suffix=page_label_suffix,
                                   manual_bbox_norm=manual_bbox_norm)


def _review_item_confirm_text(item: dict) -> str:
    """将确认为“对”的系统审核问题整理成人工批注文本."""
    rule_id = item.get("规则编号", "") or "未编号"
    severity = item.get("严重程度", "") or "待复核"
    location = item.get("部位", "") or "部位未明确"
    description = item.get("问题描述", "") or "问题描述为空"
    suggestion = item.get("建议", "")
    lines = [f"[{rule_id}] {severity}", f"部位：{location}", f"问题：{description}"]
    if suggestion:
        lines.append(f"建议：{suggestion}")
    return "\n".join(lines)


def _render_page_review_items(doc: Document, page_number: int, page_items: list[dict],
                              page_label_suffix: str = ""):
    """按 1、2、3 分条展示当前页系统审核问题，支持人工对错确认."""
    header = f"第 {page_number} 页"
    if page_label_suffix:
        header = f"{header} {page_label_suffix}"
    st.markdown(f"#### 🔍 {header} · 系统审核")
    if not page_items:
        st.caption("本页暂无匹配到页码的系统审核问题。")
        return

    for idx, item in enumerate(page_items, start=1):
        severity = item.get("严重程度", "") or "待复核"
        rule_id = item.get("规则编号", "") or "未编号"
        location = item.get("部位", "") or "部位未明确"
        description = item.get("问题描述", "") or "问题描述为空"
        suggestion = item.get("建议", "")
        verdict_key = f"review_verdict_{doc.id}_{page_number}_{idx}"
        verdict = st.session_state.get(verdict_key)

        def _render_detail():
            st.markdown(f"**{idx}. [{rule_id}] {severity}**")
            st.markdown(f"部位：{location}")
            st.markdown(f"问题：{description}")
            if suggestion:
                st.markdown(f"建议：{suggestion}")

        with st.container(border=True):
            summary = f"{idx}. [{rule_id}] {severity} 部位：{location} 问题：{description}"
            if len(summary) <= 50:
                _render_detail()
            else:
                st.markdown(summary[:50] + "…")
                with st.expander("展开", expanded=False):
                    _render_detail()

            if verdict == "correct":
                st.success("已确认成立，已加入人工批注")
            elif verdict == "wrong":
                st.info("已标记为不成立")

            focus_key = f"review_focus_bbox_{doc.id}_{page_number}"
            bbox = _parse_review_item_bbox(item)
            is_focused = st.session_state.get(focus_key) == idx
            if bbox:
                if is_focused:
                    if st.button("↩️ 取消放大", key=f"unfocus_{doc.id}_{page_number}_{idx}", width="stretch"):
                        st.session_state.pop(focus_key, None)
                        st.rerun()
                else:
                    if st.button(f"🔍 在图上放大问题 {idx}", key=f"focus_{doc.id}_{page_number}_{idx}", width="stretch"):
                        st.session_state[focus_key] = idx
                        st.rerun()
            else:
                st.caption("该问题无图上坐标，无法定位放大。")

            c1, c2 = st.columns(2)
            if c1.button("✅ 对", key=f"correct_{doc.id}_{page_number}_{idx}"):
                if verdict != "correct":
                    _add_manual_comment(
                        doc.id, page_number, _review_item_confirm_text(item),
                        verdict="confirmed",
                        rule_code=item.get("规则编号") or None,
                        location=item.get("部位") or None,
                        problem=item.get("问题描述") or None,
                        suggestion=item.get("建议") or None,
                        severity=item.get("严重程度") or None,
                    )
                st.session_state[verdict_key] = "correct"
                st.rerun()
            if c2.button("❌ 错", key=f"wrong_{doc.id}_{page_number}_{idx}"):
                st.session_state[verdict_key] = "wrong"
                st.rerun()


def _estimate_text_area_height(text: str) -> int:
    """根据文本行数估算 text_area 高度，避免编辑时只显示部分文字."""
    text = text or ""
    explicit_lines = text.count("\n") + 1
    # 估算长行自动换行占用的额外行数（按每行约 18 个全角字符）。
    wrapped_lines = sum(max(1, (len(seg) // 18) + 1) for seg in text.split("\n"))
    lines = max(explicit_lines, wrapped_lines)
    return max(80, min(400, lines * 26 + 20))


def _render_manual_comments_ui(doc: Document, page_number: int, page_comments: list[dict],
                               page_image_path: Path | None = None,
                               page_label_suffix: str = "",
                               manual_bbox_norm: tuple[float, float, float, float] | None = None):
    """当前页人工批注的增删改界面（显示在图片右侧）。

    page_image_path: 当前页 PNG 路径；提供后新增批注支持在图上拖拽画框选定位置。
    page_label_suffix: 图名+图号后缀（例如 "A户型平面图 P00"），有则拼在标题里。
    manual_bbox_norm: 左侧大图上已选定的归一化 bbox（由左侧画布标注提供）。
    """
    header = f"第 {page_number} 页"
    if page_label_suffix:
        header = f"{header} {page_label_suffix}"
    st.markdown(f"#### 📝 {header} · 人工批注")
    st.caption("批注保存到本审核结果中，按页码记录。")

    # 新增批注区域放在列表上方，带边框
    with st.container(border=True):
        _render_add_manual_comment(doc, page_number, page_comments, page_image_path, manual_bbox_norm)

    if page_comments:
        st.markdown("**📋 已有批注**")
        # 固定高度容器，批注过多时内部上下滚动，避免页面被拉得过长
        with st.container(height=450):
            for idx, comment in enumerate(page_comments, start=1):
                comment_id = comment.get("id", "")
                author = comment.get("author", "") or "匿名用户"
                created_at = comment.get("created_at", "")
                time_str = ""
                if created_at:
                    try:
                        time_str = format_beijing(datetime.fromisoformat(created_at), "%Y-%m-%d %H:%M")
                    except Exception:
                        time_str = ""
                edit_key = f"edit_comment_{doc.id}_{page_number}_{comment_id}"
                problem_val, suggestion_val = _comment_display_parts(comment)
                with st.container(border=True):
                    st.markdown(f"**{idx}. {author}**" + (f" · {time_str}" if time_str else ""))
                    if st.session_state.get(edit_key, False):
                        new_problem = st.text_area(
                            "问题描述",
                            value=problem_val,
                            key=f"edit_problem_{doc.id}_{page_number}_{comment_id}",
                            height=_estimate_text_area_height(problem_val),
                        )
                        new_suggestion = st.text_area(
                            "建议方案",
                            value=suggestion_val,
                            key=f"edit_suggestion_{doc.id}_{page_number}_{comment_id}",
                            height=_estimate_text_area_height(suggestion_val),
                        )
                        c1, c2 = st.columns(2)
                        if c1.button("保存", key=f"save_{doc.id}_{page_number}_{comment_id}", type="primary"):
                            if not (new_problem.strip() or new_suggestion.strip()):
                                st.warning("请至少填写“问题描述”或“建议方案”其中之一。")
                            else:
                                _update_manual_comment(
                                    doc.id, page_number, comment_id,
                                    problem=new_problem, suggestion=new_suggestion,
                                )
                                st.session_state[edit_key] = False
                                st.rerun()
                        if c2.button("取消", key=f"cancel_{doc.id}_{page_number}_{comment_id}"):
                            st.session_state[edit_key] = False
                            st.rerun()
                    else:
                        if problem_val:
                            st.markdown(f"**问题描述：** {problem_val}")
                        if suggestion_val:
                            st.markdown(f"**建议方案：** {suggestion_val}")
                        if not problem_val and not suggestion_val:
                            # 极端兜底：两段都为空但历史 text 也为空的行
                            st.markdown(comment.get("text", ""))
                        bbox = comment.get("bbox")
                        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
                            st.caption(
                                "📍 已标注图上位置："
                                f"({bbox[0]*100:.0f}%, {bbox[1]*100:.0f}%) → "
                                f"({bbox[2]*100:.0f}%, {bbox[3]*100:.0f}%)"
                            )
                        # 按钮行：根据是否有 bbox 调整列分布
                        if bbox is not None:
                            c1, c2, c3 = st.columns([1, 1, 1])
                            if c1.button("✏️ 编辑", key=f"editbtn_{doc.id}_{page_number}_{comment_id}"):
                                st.session_state[edit_key] = True
                                st.rerun()
                            if c2.button("🗑️ 删除", key=f"del_{doc.id}_{page_number}_{comment_id}"):
                                _delete_manual_comment(doc.id, page_number, comment_id)
                                st.rerun()
                            # 点击在图上显示标注位置按钮
                            manual_focus_key = f"manual_focus_{doc.id}_{page_number}"
                            current_focus = st.session_state.get(manual_focus_key)
                            is_focused = current_focus == comment_id
                            if c3.button("📍 查看位置" if not is_focused else "❌ 取消定位",
                                       key=f"focus_{doc.id}_{page_number}_{comment_id}"):
                                if is_focused:
                                    st.session_state.pop(manual_focus_key, None)
                                else:
                                    st.session_state[manual_focus_key] = comment_id
                                st.rerun()
                        else:
                            c1, c2 = st.columns(2)
                            if c1.button("✏️ 编辑", key=f"editbtn_{doc.id}_{page_number}_{comment_id}"):
                                st.session_state[edit_key] = True
                                st.rerun()
                            if c2.button("🗑️ 删除", key=f"del_{doc.id}_{page_number}_{comment_id}"):
                                _delete_manual_comment(doc.id, page_number, comment_id)
                                st.rerun()
    else:
        st.caption("本页暂无人工批注。")


def _render_add_manual_comment(doc: Document, page_number: int, page_comments: list[dict],
                               page_image_path: Path | None,
                               manual_bbox_norm: tuple[float, float, float, float] | None = None):
    """新增批注：问题描述 + 建议方案 + 可选的“图上拖拽画框”定位。

    标注位置现在改到左侧大图画布进行，这里只接收结果。
    """
    # 标题行：左侧标题，右侧标注位置按钮
    annotate_mode_key = f"annotate_mode_{doc.id}_{page_number}"
    annotate_mode = st.session_state.get(annotate_mode_key, False)
    col_title, col_annotate = st.columns([3, 1.2])
    with col_title:
        st.markdown("**➕ 新增人工批注**")
    with col_annotate:
        if CANVAS_AVAILABLE:
            if not annotate_mode:
                if st.button("✏️ 标注位置", key=f"start_annotate_{doc.id}_{page_number}"):
                    st.session_state[annotate_mode_key] = True
                    st.rerun()
            else:
                if st.button("❌ 退出标注", key=f"exit_annotate_{doc.id}_{page_number}"):
                    st.session_state[annotate_mode_key] = False
                    st.rerun()

    if not CANVAS_AVAILABLE:
        st.caption("💡 提示：安装 `streamlit-drawable-canvas` 后可在左侧大图上拖拽画框选定批注位置。")
    elif manual_bbox_norm:
        st.caption(
            f"📍 已在左侧大图标注位置：({manual_bbox_norm[0]*100:.0f}%, {manual_bbox_norm[1]*100:.0f}%) → "
            f"({manual_bbox_norm[2]*100:.0f}%, {manual_bbox_norm[3]*100:.0f}%)"
        )
    else:
        st.caption("💡 点击右侧「标注位置」后在大图上拖拽画框可标注问题位置（不需要标注位置时直接填写内容添加即可）。")

    # 用 form + clear_on_submit=True，提交成功后 Streamlit 自动清空表单内所有输入。
    form_key = f"new_comment_form_{doc.id}_{page_number}"
    with st.form(key=form_key, clear_on_submit=True):
        new_problem = st.text_area(
            "问题描述",
            placeholder="描述在图上发现的问题（例如：卫生间防水返高不足）...",
            height=80,
            key=f"new_comment_problem_{doc.id}_{page_number}",
        )
        new_suggestion = st.text_area(
            "建议方案",
            placeholder="给出处理/整改建议（例如：将防水返高至 300mm）...",
            height=80,
            key=f"new_comment_suggestion_{doc.id}_{page_number}",
        )
        submitted = st.form_submit_button("➕ 添加批注", type="primary")

    if submitted:
        if not (new_problem.strip() or new_suggestion.strip()):
            st.warning("请至少填写“问题描述”或“建议方案”其中之一。")
            return
        _add_manual_comment(
            doc.id, page_number, text="",
            problem=new_problem, suggestion=new_suggestion,
            bbox=manual_bbox_norm,
        )
        # 添加成功后清除已标注的 bbox，并自动退出标注模式
        manual_bbox_key = f"manual_bbox_{doc.id}_{page_number}"
        st.session_state.pop(manual_bbox_key, None)
        st.session_state.pop(f"annotate_mode_{doc.id}_{page_number}", None)
        st.rerun()


def _canvas_display_size(width: int, height: int, max_width: int = 520, max_height: int = 520) -> tuple[int, int]:
    """在保持长宽比的前提下把大图缩到可绘制画布尺寸，返回 (画布宽, 画布高)."""
    if width <= 0 or height <= 0:
        return max_width, max_height
    scale = min(max_width / width, max_height / height, 1.0)
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def _bbox_to_canvas_rect(bbox: list | tuple, canvas_w: int, canvas_h: int, stroke_color: str = "#22c55e") -> dict:
    """把归一化 bbox 转成 st_canvas initial_drawing 里的一个矩形对象."""
    x1, y1, x2, y2 = (float(v) for v in bbox[:4])
    return {
        "type": "rect",
        "left": x1 * canvas_w,
        "top": y1 * canvas_h,
        "width": max(1.0, (x2 - x1) * canvas_w),
        "height": max(1.0, (y2 - y1) * canvas_h),
        "fill": "rgba(34, 197, 94, 0.10)",
        "stroke": stroke_color,
        "strokeWidth": 2,
        "selectable": False,
        "evented": False,
        "hoverCursor": "default",
    }


def _render_manual_bbox_picker(doc: Document, page_number: int, page_comments: list[dict],
                               page_image_path: Path | None) -> tuple[float, float, float, float] | None:
    """在图上拖拽画一个矩形选定批注位置；返回归一化 bbox 或 None."""
    if page_image_path is None or not page_image_path.exists():
        return None
    if not CANVAS_AVAILABLE:
        st.caption("💡 提示：安装 `streamlit-drawable-canvas` 后可在图上拖拽画框选定批注位置。")
        return None

    try:
        with Image.open(page_image_path) as bg:
            bg = bg.convert("RGB")
            orig_w, orig_h = bg.size
            canvas_w, canvas_h = _canvas_display_size(orig_w, orig_h)
            preview = bg.resize((canvas_w, canvas_h))
    except Exception:
        return None

    # 把当前页已有的人工批注框作为绿色只读矩形叠在背景上，避免用户重复标注
    existing_rects = []
    for c in page_comments:
        b = c.get("bbox")
        if isinstance(b, (list, tuple)) and len(b) == 4:
            existing_rects.append(_bbox_to_canvas_rect(b, canvas_w, canvas_h, stroke_color="#22c55e"))

    st.caption("在下方图上按住鼠标**拖拽画一个矩形**来标注问题位置（已有的绿色框为历史批注）。不需要画时留空即可。")
    canvas_result = st_canvas(
        fill_color="rgba(239, 68, 68, 0.12)",
        stroke_width=2,
        stroke_color="#ef4444",
        background_image=preview,
        update_streamlit=True,
        height=canvas_h,
        width=canvas_w,
        drawing_mode="rect",
        display_toolbar=True,
        initial_drawing={"version": "4.4.0", "objects": existing_rects} if existing_rects else None,
        key=f"canvas_{doc.id}_{page_number}",
    )

    # 取最后一个由用户新画的矩形（初始的绿色矩形是 selectable=False，不会出现在这里）
    if canvas_result is None or canvas_result.json_data is None:
        return None
    new_rects = [
        obj for obj in canvas_result.json_data.get("objects", [])
        if obj.get("type") == "rect" and obj.get("selectable", True)
    ]
    if not new_rects:
        return None
    last = new_rects[-1]
    left = float(last.get("left", 0))
    top = float(last.get("top", 0))
    w = float(last.get("width", 0)) * float(last.get("scaleX", 1) or 1)
    h = float(last.get("height", 0)) * float(last.get("scaleY", 1) or 1)
    if w <= 0 or h <= 0:
        return None
    x1 = left / canvas_w
    y1 = top / canvas_h
    x2 = (left + w) / canvas_w
    y2 = (top + h) / canvas_h
    bbox = _normalize_manual_bbox((x1, y1, x2, y2))
    if bbox:
        st.caption(
            f"✅ 已选定位置：({bbox[0]*100:.0f}%, {bbox[1]*100:.0f}%) → "
            f"({bbox[2]*100:.0f}%, {bbox[3]*100:.0f}%)"
        )
    return bbox


def _render_full_resolution_image(image_path: Path, caption: str, key: str,
                                  focus_bbox: tuple[float, float, float, float] | None = None):
    """在图片上方提供查看原图入口，并内嵌可缩放/拖动的图片查看器。

    focus_bbox 为归一化 (x1,y1,x2,y2)（[0,1]）时，加载后自动放大并居中到该区域。
    """
    try:
        with Image.open(image_path) as img:
            width, height = img.size
    except Exception:
        st.image(str(image_path), caption=caption, width="stretch")
        return

    data = base64.b64encode(image_path.read_bytes()).decode("ascii")
    safe_key = re.sub(r"[^a-zA-Z0-9_-]", "_", key)
    focus_json = json.dumps(list(focus_bbox)) if focus_bbox else "null"
    if focus_bbox:
        st.caption(f"{caption} · 原始 {width} × {height}px · 已自动定位到所选问题；点“复位”查看整页，滚轮/按钮可缩放。")
    else:
        st.caption(f"{caption} · 原始 {width} × {height}px · 使用按钮或滚轮缩放，可拖动查看。")
    st.iframe(
        f"""
<div style="font-family:sans-serif;">
  <div style="display:flex; gap:8px; align-items:center; margin-top:4px; margin-bottom:8px; flex-wrap:wrap;">
    <button id="open-{safe_key}" style="padding:0.35rem 0.6rem; border:1px solid #d1d5db; border-radius:0.5rem; background:#fff; cursor:pointer;">🔎 查看原图</button>
    <button id="zoomout-{safe_key}" style="padding:0.35rem 0.6rem; border:1px solid #d1d5db; border-radius:0.5rem; background:#fff; cursor:pointer;">➖</button>
    <button id="zoomin-{safe_key}" style="padding:0.35rem 0.6rem; border:1px solid #d1d5db; border-radius:0.5rem; background:#fff; cursor:pointer;">➕</button>
    <button id="reset-{safe_key}" style="padding:0.35rem 0.6rem; border:1px solid #d1d5db; border-radius:0.5rem; background:#fff; cursor:pointer;">复位</button>
    <span id="pct-{safe_key}" style="color:#6b7280; font-size:0.9rem;">100%</span>
  </div>
  <div id="frame-{safe_key}" style="border:1px solid #e5e7eb; border-radius:8px; background:#f9fafb; overflow:auto; max-height:864px; cursor:grab; position:relative;">
    <img id="img-{safe_key}" src="data:image/png;base64,{data}" style="display:block; transform-origin:top left; width:{width}px; height:{height}px;" />
  </div>
</div>
<script>
(function() {{
  const base64 = '{data}';
  const focus = {focus_json};
  const img = document.getElementById('img-{safe_key}');
  const frame = document.getElementById('frame-{safe_key}');
  const pct = document.getElementById('pct-{safe_key}');
  const baseW = {width}, baseH = {height};
  let scale = 1;

  function frameSize() {{
    return [frame.clientWidth - 2 || baseW, frame.clientHeight - 2 || 640];
  }}
  function fit() {{
    const avail = frame.clientWidth - 2;
    if (avail > 0 && baseW > 0) {{
      scale = Math.min(1, avail / baseW);
    }}
    apply();
  }}
  function apply() {{
    img.style.width = (baseW * scale) + 'px';
    img.style.height = (baseH * scale) + 'px';
    pct.textContent = Math.round(scale * 100) + '%';
  }}
  function zoom(factor, centerX = null, centerY = null) {{
    // 缩放时保持中心点不变
    // centerX/centerY: 相对于视口的中心点坐标，如果为 null 则使用视口中心
    const [fw, fh] = frameSize();
    const oldScale = scale;
    const newScale = Math.min(8, Math.max(0.05, scale * factor));
    if (newScale === oldScale) return;

    // 获取中心点在原图上的坐标（缩放前）
    const cxImg = centerX !== null ? (frame.scrollLeft + centerX) : (frame.scrollLeft + fw / 2);
    const cyImg = centerY !== null ? (frame.scrollTop + centerY) : (frame.scrollTop + fh / 2);

    // 应用新缩放
    scale = newScale;
    apply();

    // 调整滚动位置，让同一点仍在原来的视口位置
    const factorRatio = newScale / oldScale;
    const newCxImg = cxImg * factorRatio;
    const newCyImg = cyImg * factorRatio;
    const newScrollLeft = centerX !== null ? (newCxImg - centerX) : (newCxImg - fw / 2);
    const newScrollTop = centerY !== null ? (newCyImg - centerY) : (newCyImg - fh / 2);
    frame.scrollLeft = Math.max(0, newScrollLeft);
    frame.scrollTop = Math.max(0, newScrollTop);
  }}
  function focusOn() {{
    if (!focus) {{ fit(); return; }}
    const bx1 = focus[0] * baseW, by1 = focus[1] * baseH;
    const bx2 = focus[2] * baseW, by2 = focus[3] * baseH;
    const bw = Math.max(1, bx2 - bx1), bh = Math.max(1, by2 - by1);
    const [fw, fh] = frameSize();
    // 让问题框约占视口 55%，并限制在合理缩放范围
    let s = Math.min(fw / bw, fh / bh) * 0.55;
    s = Math.min(8, Math.max(0.1, s));
    scale = s;
    apply();
    const cx = ((bx1 + bx2) / 2) * scale, cy = ((by1 + by2) / 2) * scale;
    frame.scrollLeft = Math.max(0, cx - fw / 2);
    frame.scrollTop = Math.max(0, cy - fh / 2);
  }}

  document.getElementById('zoomin-{safe_key}').addEventListener('click', () => zoom(1.25));
  document.getElementById('zoomout-{safe_key}').addEventListener('click', () => zoom(0.8));
  document.getElementById('reset-{safe_key}').addEventListener('click', fit);

  frame.addEventListener('wheel', function(e) {{
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    // 以鼠标位置为中心缩放
    const rect = frame.getBoundingClientRect();
    const mouseX = e.clientX - rect.left;
    const mouseY = e.clientY - rect.top;
    zoom(e.deltaY < 0 ? 1.1 : 0.9, mouseX, mouseY);
  }}, {{ passive: false }});

  // Drag to pan
  let dragging = false, sx = 0, sy = 0, sl = 0, stp = 0;
  frame.addEventListener('mousedown', function(e) {{
    dragging = true; sx = e.clientX; sy = e.clientY; sl = frame.scrollLeft; stp = frame.scrollTop;
    frame.style.cursor = 'grabbing';
  }});
  window.addEventListener('mouseup', function() {{ dragging = false; frame.style.cursor = 'grab'; }});
  window.addEventListener('mousemove', function(e) {{
    if (!dragging) return;
    frame.scrollLeft = sl - (e.clientX - sx);
    frame.scrollTop = stp - (e.clientY - sy);
  }});

  document.getElementById('open-{safe_key}').addEventListener('click', function() {{
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) {{ bytes[i] = binary.charCodeAt(i); }}
    const blob = new Blob([bytes], {{ type: 'image/png' }});
    const url = URL.createObjectURL(blob);
    window.open(url, '_blank', 'noopener,noreferrer');
  }});

  // 初次渲染：有定位框则放大居中，否则自适应
  if (focus) {{ setTimeout(focusOn, 30); }} else {{ fit(); }}
  window.addEventListener('resize', function() {{ if (focus) focusOn(); else fit(); }});
}})();
</script>
""",
        height=972,
    )


def _markdown_table_cell(value: object) -> str:
    """清理 Markdown 表格单元格内容，避免导出表格断列."""
    return str(value or "").replace("|", "\\|").replace("\n", "<br>")



def _export_review_as_markdown(doc: Document, review_items: list[dict]) -> str:
    """生成审核结果 Markdown 导出内容."""
    lines = []
    lines.append("# 图纸审核报告")
    lines.append("")
    lines.append(f"**图纸文件:** {doc.filename}")
    lines.append(f"**项目 ID:** {doc.project_id}")
    if doc.analyzed_at:
        lines.append(f"**审核时间:** {format_beijing(doc.analyzed_at, '%Y-%m-%d %H:%M:%S')}")
    _analysis_data = _parse_analysis_data(doc)
    _duration_txt = _format_duration_seconds(_analysis_data.get("duration_seconds"))
    if _duration_txt:
        lines.append(f"**AI 用时:** {_duration_txt}")
    lines.append("")

    if review_items:
        lines.append("## 审核提疑汇总")
        lines.append("")
        lines.append("| 规则编号 | 部位 | 问题描述 | 涉及图纸 | 严重程度 | 建议 | 可信度 |")
        lines.append("|----------|------|----------|----------|----------|------|--------|")
        for item in review_items:
            cells = [
                _markdown_table_cell(item.get("规则编号", "")),
                _markdown_table_cell(item.get("部位", "")),
                _markdown_table_cell(item.get("问题描述", "")),
                _markdown_table_cell(item.get("涉及图纸", "")),
                _markdown_table_cell(item.get("严重程度", "")),
                _markdown_table_cell(item.get("建议", "")),
                _markdown_table_cell(item.get("可信度", "")),
            ]
            lines.append(f"| {' | '.join(cells)} |")
        lines.append("")

    lines.append("## 完整审核原文")
    lines.append("")
    lines.append(doc.analysis_summary or "")
    return "\n".join(lines)


def _export_review_as_csv(review_items: list[dict]) -> str:
    """生成审核提疑 CSV 导出内容."""
    columns = ["规则编号", "部位", "问题描述", "涉及图纸", "严重程度", "建议", "可信度"]
    lines = [",".join(f'"{col}"' for col in columns)]
    for item in review_items:
        cells = []
        for col in columns:
            value = str(item.get(col, "")).replace('"', '""')
            cells.append(f'"{value}"')
        lines.append(",".join(cells))
    return "﻿" + "\n".join(lines)


def _export_manual_comments_xlsx(doc: Document, analysis_data: dict) -> bytes | None:
    """将所有页的人工批注导出为 Excel 字节流；无批注时返回 None.

    导出列（10 列）：
      问题编号 · 图纸名称/图号 · 问题描述 · 图片附件（嵌入本页缩略图）·
      建议方案 · 回复意见 · 回复时间 · 提疑人 · 状态 · 备注
    未来在 UI 里加了「回复意见/时间/状态/备注」等字段，会自动生效；
    当前未存的字段留空，供外部在 Excel 中手工补写。
    """
    comments_by_page = _load_manual_comments(analysis_data)
    if not comments_by_page:
        return None

    def _page_num(key: str) -> int:
        try:
            return int(str(key).replace("page_", ""))
        except ValueError:
            return 0

    # 预处理页图像路径，用于图片附件嵌入
    preprocessed = [Path(p) for p in analysis_data.get("preprocessed_images", []) if Path(p).exists()]
    page_labels = _load_page_labels(analysis_data)

    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment
    from openpyxl.drawing.image import Image as XLImage

    wb = Workbook()
    ws = wb.active
    ws.title = "人工批注"
    headers = [
        "问题编号", "图纸名称/图号", "问题描述", "图片附件",
        "建议方案", "回复意见", "回复时间", "提疑人", "状态", "备注",
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")

    # 列宽（图片列相对宽一点以容纳嵌入的原图缩略）
    widths = [12, 22, 42, 38, 42, 30, 18, 14, 12, 24]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[chr(64 + i)].width = w

    # 图片附件：优先每条批注一张（带 bbox 的画在图上），无 bbox 的按页共用一张原图。
    # 存在 xlsx_attachments/ 下便于复用；保留完整原图分辨率（Excel 显示时会等比缩放，双击可查看原图）。
    attach_dir = Path("data") / "projects" / str(doc.project_id) / "annotated_drawings" / str(doc.id) / "xlsx_attachments"
    attach_cache: dict[str, Path] = {}

    def _attachment_for(page_number: int, comment: dict) -> Path | None:
        """返回本条批注对应的原图附件；有 bbox 就在原图上画出，无 bbox 就用整页原图.

        缓存 key 兼顾去重：无 bbox 用页号；有 bbox 用 comment_id，避免不同 bbox 的批注共用同一张图。
        """
        if not (1 <= page_number <= len(preprocessed)):
            return None
        bbox_raw = comment.get("bbox")
        bbox: tuple[float, float, float, float] | None = None
        if isinstance(bbox_raw, (list, tuple)) and len(bbox_raw) == 4:
            try:
                bbox = tuple(float(v) for v in bbox_raw)  # type: ignore[assignment]
            except (TypeError, ValueError):
                bbox = None

        cache_key = (
            f"p{page_number:03d}_c{comment.get('id', 'x')}"
            if bbox else f"p{page_number:03d}_plain"
        )
        if cache_key in attach_cache:
            return attach_cache[cache_key]

        src = preprocessed[page_number - 1]
        attach_dir.mkdir(parents=True, exist_ok=True)
        dest = attach_dir / f"{cache_key}.png"
        try:
            if not dest.exists():
                with Image.open(src) as img:
                    base = img.convert("RGB")
                    if bbox is not None:
                        # 保留原图分辨率，直接在原图上画标注
                        w, h = base.size
                        x1, y1, x2, y2 = bbox
                        # 夹紧到 [0,1] 并转成像素
                        x1p = int(max(0.0, min(1.0, min(x1, x2))) * w)
                        y1p = int(max(0.0, min(1.0, min(y1, y2))) * h)
                        x2p = int(max(0.0, min(1.0, max(x1, x2))) * w)
                        y2p = int(max(0.0, min(1.0, max(y1, y2))) * h)
                        if x2p > x1p and y2p > y1p:
                            annotated = base.copy()
                            drw = ImageDraw.Draw(annotated, "RGBA")
                            # 描边宽度按图大小自适应（大图不刺眼、小图看得清）
                            line_w = max(3, round(min(w, h) / 300))
                            # 半透明填充 + 实线边框（跟 UI 里的画框选区色调保持一致 #22c55e）
                            drw.rectangle(
                                [x1p, y1p, x2p, y2p],
                                fill=(34, 197, 94, 60),
                                outline=(22, 163, 74, 255),
                                width=line_w,
                            )
                            annotated.save(dest, format="PNG")
                        else:
                            base.save(dest, format="PNG")
                    else:
                        base.save(dest, format="PNG")
        except Exception:
            return None

        attach_cache[cache_key] = dest
        return dest

    row_idx = 2  # 表头是第 1 行
    global_seq = 0
    for page_key in sorted(comments_by_page.keys(), key=_page_num):
        page_num = _page_num(page_key)
        page_comments = comments_by_page.get(page_key) or []
        # 图纸名称/图号：优先 图名+图号，其次仅图号，最后 "第N页"
        label_suffix = _format_page_label(page_labels, page_num)
        sheet_label = label_suffix or f"第 {page_num} 页"

        for local_idx, comment in enumerate(page_comments, start=1):
            global_seq += 1
            problem_val, suggestion_val = _comment_display_parts(comment)
            created_at = comment.get("created_at", "")
            time_str = ""
            if created_at:
                try:
                    time_str = format_beijing(datetime.fromisoformat(created_at), "%Y-%m-%d %H:%M")
                except Exception:
                    time_str = created_at

            issue_code = f"P{page_num:02d}-{local_idx:02d}"  # 例如 P13-01
            author = comment.get("author", "") or "匿名用户"

            # 数据模型里目前没有的字段留空，导出后可手工填写
            reply_text = comment.get("reply_text", "") or ""
            reply_time = comment.get("reply_time", "") or ""
            status = comment.get("status", "") or "待回复"
            note = comment.get("note", "") or ""

            ws.cell(row=row_idx, column=1, value=issue_code)
            ws.cell(row=row_idx, column=2, value=sheet_label)
            ws.cell(row=row_idx, column=3, value=problem_val)
            # column=4 图片附件，稍后嵌入
            ws.cell(row=row_idx, column=5, value=suggestion_val)
            ws.cell(row=row_idx, column=6, value=reply_text)
            ws.cell(row=row_idx, column=7, value=reply_time)
            ws.cell(row=row_idx, column=8, value=author)
            ws.cell(row=row_idx, column=9, value=status)
            ws.cell(row=row_idx, column=10, value=note if note else f"批注时间：{time_str}")

            # 行高足够放下缩略图；Excel 单位 pt ≈ 1.333 px
            ws.row_dimensions[row_idx].height = 140

            # 嵌入图片附件到 D 列（原图分辨率，Excel 显示时按 xl_img.width/height 等比缩放）
            attachment_path = _attachment_for(page_num, comment)
            if attachment_path is not None and attachment_path.exists():
                try:
                    xl_img = XLImage(str(attachment_path))
                    # 保持原图长宽比，按行高缩放到显示尺寸；文件里图片仍是原分辨率
                    display_h = 180
                    with Image.open(attachment_path) as _probe:
                        w_px, h_px = _probe.size
                    display_w = max(80, int(round(w_px / max(1, h_px) * display_h)))
                    display_w = min(display_w, 260)  # 单元格宽度上限（D 列 24 ≈ 168px 起）
                    xl_img.width = display_w
                    xl_img.height = display_h
                    xl_img.anchor = f"D{row_idx}"
                    ws.add_image(xl_img)
                except Exception:
                    # 图片嵌入失败退化为文件名
                    ws.cell(row=row_idx, column=4, value=attachment_path.name)

            # 自动换行 + 顶部对齐（问题/建议/回复/备注 各列）
            for col in (2, 3, 5, 6, 10):
                ws.cell(row=row_idx, column=col).alignment = Alignment(
                    wrap_text=True, vertical="top",
                )
            for col in (1, 7, 8, 9):
                ws.cell(row=row_idx, column=col).alignment = Alignment(
                    horizontal="center", vertical="center",
                )

            row_idx += 1

    if row_idx == 2:
        # 表头之外没有数据
        return None

    # 冻结首行
    ws.freeze_panes = "A2"

    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _render_document_result(doc: Document):
    """按类型渲染文档分析/审核结果."""
    analysis_data = _parse_analysis_data(doc)
    is_review = analysis_data.get("job_kind") == "drawing_review"

    if is_review:
        st.subheader("✅ 审核结果")
        review_items = _extract_standard_review_items(doc.analysis_summary or "")
        if review_items:
            _render_review_items_table(review_items)

            with st.expander("🖼️ 查看审核标注图", expanded=True):
                st.caption("自动在图纸右侧生成审核问题编号图例；编号与下方标准提疑表顺序一致。")
                _render_review_legend_images(doc, review_items, analysis_data)

            with st.expander("查看审核原文", expanded=False):
                st.write(doc.analysis_summary)

            # 导出功能区域
            st.divider()
            col1, col2, col3, col4 = st.columns([1, 1, 1, 1])
            with col1:
                # 导出 Markdown
                md_content = _export_review_as_markdown(doc, review_items)
                timestamp = format_beijing(doc.analyzed_at, '%Y%m%d_%H%M%S') if doc.analyzed_at else "export"
                filename_md = f"{Path(doc.filename).stem}_审核结果_{timestamp}.md"
                st.download_button(
                    label="📄 导出 Markdown",
                    data=md_content,
                    file_name=filename_md,
                    mime="text/markdown",
                    key=f"export_md_{doc.id}",
                )
            with col2:
                # 导出 CSV 提疑表格
                csv_content = _export_review_as_csv(review_items)
                filename_csv = f"{Path(doc.filename).stem}_审核提疑_{timestamp}.csv"
                st.download_button(
                    label="📊 导出 CSV 提疑表",
                    data=csv_content,
                    file_name=filename_csv,
                    mime="text/csv",
                    key=f"export_csv_{doc.id}",
                )
            with col3:
                # 导出人工批注 Excel
                xlsx_bytes = _export_manual_comments_xlsx(doc, analysis_data)
                st.download_button(
                    label="📝 导出人工批注 Excel",
                    data=xlsx_bytes or b"",
                    file_name=f"{Path(doc.filename).stem}_人工批注_{timestamp}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key=f"export_comments_xlsx_{doc.id}",
                    disabled=xlsx_bytes is None,
                    help=None if xlsx_bytes else "暂无人工批注可导出",
                )
            with col4:
                # 导出人工批注结果（图纸+批注拼接后合成多页PDF）
                if st.button(
                    "📑 导出批注结果 PDF",
                    key=f"gen_comments_pdf_{doc.id}",
                    help="将每页图纸与人工批注拼接，合成一个多页PDF",
                ):
                    with st.spinner("正在生成批注结果 PDF..."):
                        st.session_state[f"comments_pdf_{doc.id}"] = _export_comment_annotated_pdf(doc, analysis_data)
                pdf_bytes = st.session_state.get(f"comments_pdf_{doc.id}")
                if pdf_bytes:
                    st.download_button(
                        label="⬇️ 下载批注结果 PDF",
                        data=pdf_bytes,
                        file_name=f"{Path(doc.filename).stem}_批注结果_{timestamp}.pdf",
                        mime="application/pdf",
                        key=f"download_comments_pdf_{doc.id}",
                    )
                elif pdf_bytes is not None:
                    st.caption("无可用于生成的预处理图片。")
        else:
            st.info("未能从审核结果中解析出标准提疑表格，以下展示审核原文。")
            st.write(doc.analysis_summary)
            # 仍然提供导出原文为 Markdown
            st.divider()
            md_content = _export_review_as_markdown(doc, [])
            timestamp = format_beijing(doc.analyzed_at, '%Y%m%d_%H%M%S') if doc.analyzed_at else "export"
            filename_md = f"{Path(doc.filename).stem}_审核结果_{timestamp}.md"
            st.download_button(
                label="📄 导出 Markdown 原文",
                data=md_content,
                file_name=filename_md,
                mime="text/markdown",
                key=f"export_md_raw_{doc.id}",
            )
    else:
        st.subheader("📝 分析摘要")
        st.write(doc.analysis_summary)
        # 普通分析也支持导出
        st.divider()
        md_content = f"# 图纸分析报告\n\n**图纸文件:** {doc.filename}\n\n## 分析摘要\n\n{doc.analysis_summary or ''}"
        timestamp = format_beijing(doc.analyzed_at, '%Y%m%d_%H%M%S') if doc.analyzed_at else "export"
        filename_md = f"{Path(doc.filename).stem}_分析结果_{timestamp}.md"
        st.download_button(
            label="📄 导出 Markdown",
            data=md_content,
            file_name=filename_md,
            mime="text/markdown",
            key=f"export_md_analysis_{doc.id}",
        )

    chat_session_id = analysis_data.get("chat_session_id")
    if chat_session_id:
        with st.expander("💬 继续问答分析（保留当前对话进度）", expanded=False):
            _render_inline_chat(
                session_id=chat_session_id,
                project_id=doc.project_id,
                key_prefix=f"doc_{doc.id}_inline_chat",
                default_provider=analysis_data.get("provider"),
            )

    if analysis_data:
        with st.expander("查看详细分析数据"):
            st.json(analysis_data)


def _format_duration_seconds(seconds: float | int | None) -> str:
    """把秒数格式化为易读时长（如 3.4 秒 / 1 分 12 秒 / 1 小时 03 分）。"""
    if seconds is None:
        return ""
    try:
        secs = float(seconds)
    except (TypeError, ValueError):
        return ""
    if secs < 0:
        return ""
    if secs < 60:
        return f"{secs:.1f} 秒"
    if secs < 3600:
        m, s = divmod(int(round(secs)), 60)
        return f"{m} 分 {s:02d} 秒"
    h, rem = divmod(int(round(secs)), 3600)
    m = rem // 60
    return f"{h} 小时 {m:02d} 分"


def _save_doc_analysis_result(doc_id: int, answer: str, data: dict):
    """保存图纸分析结果到 Document 分析字段."""
    with session() as s:
        doc_db = s.query(Document).filter(Document.id == doc_id).first()
        if doc_db:
            doc_db.analysis_status = "done"
            doc_db.analysis_summary = answer
            doc_db.analysis_data_json = json.dumps(data, ensure_ascii=False, indent=2)
            doc_db.analysis_error = None
            doc_db.analyzed_at = now_utc()
            s.commit()


def _mark_doc_failed(doc_id: int, error: str):
    """标记图纸分析失败."""
    with session() as s:
        doc_db = s.query(Document).filter(Document.id == doc_id).first()
        if doc_db:
            doc_db.analysis_status = "failed"
            doc_db.analysis_error = error
            s.commit()


def _clear_review_history(project_id: int) -> tuple[int, int]:
    """清空图纸审核历史，不影响普通图纸分析会话."""
    with session() as s:
        review_sessions = (
            s.query(ChatSession)
            .filter(
                ChatSession.project_id == project_id,
                ChatSession.title.like(f"{REVIEW_SESSION_PREFIX}%"),
            )
            .all()
        )
        deleted_sessions = len(review_sessions)
        for chat_sess in review_sessions:
            s.delete(chat_sess)

        cleared_docs = 0
        docs = s.query(Document).filter(Document.project_id == project_id).all()
        for doc in docs:
            data = {}
            if doc.analysis_data_json:
                try:
                    data = json.loads(doc.analysis_data_json)
                except json.JSONDecodeError:
                    data = {}
            if data.get("job_kind") != "drawing_review":
                continue
            doc.analysis_status = "pending"
            doc.analysis_summary = None
            doc.analysis_data_json = None
            doc.analysis_error = None
            doc.analyzed_at = None
            cleared_docs += 1

        s.commit()
        return deleted_sessions, cleared_docs


def _run_drawing_ai_job(
    project: Project,
    doc: Document,
    provider: str,
    title: str,
    prompt: str,
    job_kind: str,
    extra_data: dict | None = None,
) -> dict:
    """预处理图纸并调用视觉AI，返回会话结果."""
    t_start = time.perf_counter()
    with session() as s:
        doc_db = s.query(Document).filter(Document.id == doc.id).first()
        if doc_db:
            doc_db.analysis_status = "running"
            doc_db.analysis_error = None
            s.commit()

    t_pre_start = time.perf_counter()
    image_paths, error = _prepare_drawing_images(doc, project.id)
    preprocess_seconds = time.perf_counter() - t_pre_start
    if error:
        _mark_doc_failed(doc.id, error)
        return {"error": error}

    t_ai_start = time.perf_counter()
    result = vision_chat(
        query=prompt,
        image_paths=[str(p) for p in image_paths],
        session_id=None,
        project_id=project.id,
        user_id=_user_id(),
        provider=provider,
        title=title,
    )
    ai_seconds = time.perf_counter() - t_ai_start
    if result.get("error"):
        _mark_doc_failed(doc.id, result["error"])
        return result

    total_seconds = time.perf_counter() - t_start
    analysis_data = {
        "job_kind": job_kind,
        "provider": provider,
        "source_document_id": doc.id,
        "source_document": doc.filename,
        "preprocessed_images": [str(p) for p in image_paths],
        "chat_session_id": result["session_id"],
        "chat_message_id": result.get("message_id"),
        "duration_seconds": round(total_seconds, 2),
        "duration_breakdown": {
            "preprocess_seconds": round(preprocess_seconds, 2),
            "ai_seconds": round(ai_seconds, 2),
        },
    }
    if extra_data:
        analysis_data.update(extra_data)

    _save_doc_analysis_result(doc.id, result["answer"] or "", analysis_data)
    return {
        **result,
        "image_count": len(image_paths),
        "analysis_data": analysis_data,
        "duration_seconds": total_seconds,
    }


def _run_review_triage(image_paths: list[Path], doc: Document, provider: str) -> tuple[str, dict]:
    """第一阶段分诊：视觉识别图纸类型/专业/重点，返回 (检索查询字符串, 分诊原始数据)."""
    prompt = load_prompt("review_triage", filename=doc.filename)
    result = vision_chat(
        query=prompt,
        image_paths=[str(p) for p in image_paths],
        session_id=None,
        project_id=doc.project_id,
        user_id=_user_id(),
        provider=provider,
        title=f"图纸分诊：{doc.filename}",
    )
    if result.get("error") or not result.get("answer"):
        # 分诊失败时用文件名兜底
        return doc.filename, {"error": result.get("error")}

    answer = result["answer"].strip()
    triage: dict = {}
    # 容错解析 JSON（可能被代码块包裹）
    match = re.search(r"\{.*\}", answer, flags=re.DOTALL)
    if match:
        try:
            triage = json.loads(match.group(0))
        except Exception:
            triage = {}

    keywords: list[str] = []
    for field in ["review_focus", "key_elements", "drawing_types", "disciplines", "spaces"]:
        value = triage.get(field)
        if isinstance(value, list):
            keywords.extend(str(v) for v in value if v)
        elif isinstance(value, str) and value:
            keywords.append(value)

    query = " ".join(keywords) if keywords else doc.filename
    triage["_query"] = query
    triage["_raw"] = answer
    return query, triage


def _retrieve_review_context(project: Project, doc: Document, provider: str,
                             image_paths: list[Path], custom_prompt: str) -> dict:
    """两阶段 RAG 检索：分诊 -> 检索规则+案例。返回上下文 dict."""
    query, triage = _run_review_triage(image_paths, doc, provider)
    if custom_prompt:
        query = f"{query} {custom_prompt}"

    rules = drawing_rag.search_rules(project.id, query, top_k=15)
    cases = drawing_rag.search_cases(project.id, query, top_k=6)

    fallback = False
    if not rules:
        # RAG 未命中（例如规则尚未向量化）时回退全量规则，保证不退化
        rules = drawing_rag.get_all_rules(project.id)
        fallback = bool(rules)

    return {
        "query": query,
        "triage": triage,
        "rules": rules,
        "cases": cases,
        "fallback_all_rules": fallback,
    }


def _run_drawing_review_rag(project: Project, doc: Document, provider: str,
                            custom_prompt: str) -> dict:
    """图纸审核（RAG）：预处理 -> 分诊检索 -> 审核，并保存结果."""
    t_start = time.perf_counter()
    with session() as s:
        doc_db = s.query(Document).filter(Document.id == doc.id).first()
        if doc_db:
            doc_db.analysis_status = "running"
            doc_db.analysis_error = None
            s.commit()

    t_pre_start = time.perf_counter()
    image_paths, error = _prepare_drawing_images(doc, project.id)
    preprocess_seconds = time.perf_counter() - t_pre_start
    if error:
        _mark_doc_failed(doc.id, error)
        return {"error": error}

    t_rag_start = time.perf_counter()
    ctx = _retrieve_review_context(project, doc, provider, image_paths, custom_prompt)
    rag_seconds = time.perf_counter() - t_rag_start
    prompt = _format_review_prompt(doc, ctx["rules"], ctx["cases"], custom_prompt)

    t_ai_start = time.perf_counter()
    result = vision_chat(
        query=prompt,
        image_paths=[str(p) for p in image_paths],
        session_id=None,
        project_id=project.id,
        user_id=_user_id(),
        provider=provider,
        title=f"{REVIEW_SESSION_PREFIX}{doc.filename}",
    )
    ai_seconds = time.perf_counter() - t_ai_start
    if result.get("error"):
        _mark_doc_failed(doc.id, result["error"])
        return result

    total_seconds = time.perf_counter() - t_start

    # 提取页面名称（图名+图号），失败不阻断审核结果
    try:
        page_labels = _ensure_page_labels(doc, project.id, image_paths, use_vision=True, provider=provider)
    except Exception:
        page_labels = []

    analysis_data = {
        "job_kind": "drawing_review",
        "provider": provider,
        "source_document_id": doc.id,
        "source_document": doc.filename,
        "preprocessed_images": [str(p) for p in image_paths],
        "chat_session_id": result["session_id"],
        "chat_message_id": result.get("message_id"),
        "custom_prompt": custom_prompt,
        "rag_query": ctx["query"],
        "rag_triage": ctx["triage"],
        "rag_rules_count": len(ctx["rules"]),
        "rag_cases_count": len(ctx["cases"]),
        "rag_fallback_all_rules": ctx["fallback_all_rules"],
        "page_labels": page_labels,
        "duration_seconds": round(total_seconds, 2),
        "duration_breakdown": {
            "preprocess_seconds": round(preprocess_seconds, 2),
            "rag_retrieval_seconds": round(rag_seconds, 2),
            "ai_seconds": round(ai_seconds, 2),
        },
    }

    _save_doc_analysis_result(doc.id, result["answer"] or "", analysis_data)
    return {
        **result,
        "image_count": len(image_paths),
        "analysis_data": analysis_data,
        "rules_count": len(ctx["rules"]),
        "cases_count": len(ctx["cases"]),
        "duration_seconds": total_seconds,
    }


def view_drawing_analysis_history(project: Project):
    """查看历史图纸分析页面."""
    st.title("📋 图纸分析历史")
    st.caption(f"项目: {project.name}")

    docs = _get_drawing_documents(project)
    sessions = _drawing_sessions(project.id)
    history_docs = [d for d in docs if d.analysis_status not in ["pending", "running"]]

    if not docs:
        st.info("暂无图纸文档。请先上传图纸文件。")
        return

    if not history_docs:
        st.info("暂无历史分析结果。未分析的图纸不会显示在历史分析中，请到“新增分析”中发起分析或审核。")
        return

    total = len(history_docs)
    analyzed = sum(1 for d in history_docs if d.analysis_status == "done")
    failed = sum(1 for d in history_docs if d.analysis_status == "failed")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("历史图纸数", total)
    col2.metric("已分析", analyzed)
    col3.metric("分析失败", failed)
    col4.metric("问答会话", len(sessions))

    review_sessions_count = sum(1 for s in sessions if (s.title or "").startswith(REVIEW_SESSION_PREFIX))
    with st.expander("⚠️ 清理历史审核结果", expanded=False):
        st.caption("仅清空“图纸审核”历史：删除标题以“图纸审核：”开头的问答会话，并重置由图纸审核生成的文档结果；不会删除普通图纸分析历史。")
        confirm_clear = st.checkbox("确认清空历史审核结果", key="confirm_clear_review_history")
        if st.button(
            f"清空历史审核结果（{review_sessions_count} 个审核会话）",
            type="secondary",
            disabled=not confirm_clear,
            key="clear_review_history_btn",
        ):
            deleted_sessions, cleared_docs = _clear_review_history(project.id)
            st.session_state.pop("drawing_analysis_session_id", None)
            st.success(f"已清空：删除 {deleted_sessions} 个审核会话，重置 {cleared_docs} 条文档审核结果。")
            st.rerun()

    st.divider()

    status_filter = st.selectbox(
        "按状态筛选",
        ["全部", "已分析", "分析失败"],
        key="drawing_history_status_filter",
    )

    filtered_docs = history_docs
    if status_filter == "已分析":
        filtered_docs = [d for d in history_docs if d.analysis_status == "done"]
    elif status_filter == "分析失败":
        filtered_docs = [d for d in history_docs if d.analysis_status == "failed"]

    st.subheader(f"图纸列表 ({len(filtered_docs)})")

    for doc in filtered_docs:
        status_text = _status_text(doc.analysis_status)
        with st.expander(
            f"{_status_emoji(doc.analysis_status)} {doc.filename} · {status_text}",
            expanded=(doc.analysis_status == "done"),
        ):
            col_a, col_b = st.columns([3, 1])

            with col_a:
                upload_time = format_beijing(doc.uploaded_at, '%Y-%m-%d %H:%M:%S', fallback="未知")
                st.markdown(f"**文件名:** {doc.filename}")
                st.markdown(f"**上传时间:** {upload_time}")
                st.markdown(f"**文件大小:** {doc.size_bytes / 1024:.1f} KB")
                st.markdown(f"**分析状态:** {status_text}")
                if doc.analyzed_at:
                    st.markdown(f"**分析时间:** {format_beijing(doc.analyzed_at, '%Y-%m-%d %H:%M:%S')}")
                _analysis_data = _parse_analysis_data(doc)
                _duration_txt = _format_duration_seconds(_analysis_data.get("duration_seconds"))
                if _duration_txt:
                    st.markdown(f"**AI 用时:** {_duration_txt}")

            with col_b:
                _display_preview(project.id, doc, width=200)

            st.divider()

            if doc.analysis_status == "done" and doc.analysis_summary:
                _render_document_result(doc)

            elif doc.analysis_status == "failed" and doc.analysis_error:
                st.error(f"分析失败: {doc.analysis_error}")

            if doc.analysis_status in ["done", "failed", "unsupported", "pending"]:
                if st.button("🔄 重新分析", key=f"reanalyze_{doc.id}"):
                    with session() as s:
                        doc_db = s.query(Document).filter(Document.id == doc.id).first()
                        if doc_db:
                            doc_db.analysis_status = "pending"
                            doc_db.analysis_summary = None
                            doc_db.analysis_data_json = None
                            doc_db.analysis_error = None
                            doc_db.analyzed_at = None
                            s.commit()
                    st.success("已标记为待分析状态，请到“新增分析”中重新发起")
                    st.rerun()


def view_new_drawing_analysis(project: Project):
    """新增图纸分析页面."""
    st.title("🔍 新增图纸分析")
    st.caption(f"项目: {project.name}")

    docs = _get_drawing_documents(project)

    col_provider, col_info = st.columns([1, 2])
    with col_provider:
        providers = get_available_providers()
        default_provider = get_default_provider()
        provider = st.selectbox(
            "LLM 提供商",
            providers,
            index=providers.index(default_provider) if default_provider in providers else 0,
            key="drawing_analysis_provider",
        )
    with col_info:
        st.info("开始分析时会先预处理：PDF 转 PNG，图片直接使用；随后调用智能问答中的视觉 AI，并把结果保存为可继续追问的会话。")

    st.subheader("选择要分析的图纸")

    source_mode = st.radio(
        "图纸来源",
        ["选择已上传图纸", "上传新的图纸文件"],
        index=0 if docs else 1,
        horizontal=True,
        key="drawing_source_mode",
        help="两种方式互斥：选择已有图纸时不会上传新文件；上传新图纸时不会使用已有选择。",
    )

    selected_doc = None
    if source_mode == "选择已上传图纸":
        if not docs:
            st.info("暂无已上传图纸。请切换到“上传新的图纸文件”。")
            return

        doc_options = []
        for doc in docs:
            doc_options.append(f"{doc.filename} ({_status_text(doc.analysis_status)})")

        selected_idx = st.selectbox(
            "选择图纸文件",
            range(len(doc_options)),
            format_func=lambda i: doc_options[i],
            key="drawing_doc_selector",
        )
        selected_doc = docs[selected_idx]
    else:
        uploaded_file = st.file_uploader(
            "上传图纸文件",
            type=["pdf", "png", "jpg", "jpeg", "webp", "bmp"],
            accept_multiple_files=False,
            key="drawing_analysis_upload_file",
            help="上传后会保存到项目图纸分类，并作为本次待分析图纸。",
        )
        if st.button("上传并选择该图纸", type="primary", disabled=uploaded_file is None):
            with st.spinner("正在保存图纸文件..."):
                doc_id = _save_uploaded_drawing(project.id, uploaded_file)
            st.session_state["drawing_uploaded_selected_doc_id"] = doc_id
            with st.spinner("正在预处理并自动提取页面名称（图名+图号）..."):
                named, err = _extract_labels_after_upload(project.id, doc_id)
            if err:
                st.warning(f"页面名称自动提取失败：{err}（可在审核时手动重跑）")
            elif named:
                st.success(f"图纸已上传并选择；已自动识别 {named} 页图名/图号。")
            else:
                st.info("图纸已上传并选择；未识别到明显的图名/图号，可稍后手动重跑。")

        selected_doc_id = st.session_state.get("drawing_uploaded_selected_doc_id")
        if selected_doc_id:
            docs = _get_drawing_documents(project)
            selected_doc = next((doc for doc in docs if doc.id == selected_doc_id), None)
            if selected_doc:
                st.success(f"当前上传图纸：{selected_doc.filename}")
        if selected_doc is None:
            st.info("请先上传并选择一个图纸文件。")
            return

    st.divider()
    st.subheader("📄 图纸信息")

    col1, col2 = st.columns([1, 1])

    with col1:
        upload_time = format_beijing(selected_doc.uploaded_at, '%Y-%m-%d %H:%M:%S', fallback="未知")
        st.markdown(f"**文件名:** {selected_doc.filename}")
        st.markdown(f"**上传时间:** {upload_time}")
        st.markdown(f"**文件大小:** {selected_doc.size_bytes / 1024:.1f} KB")
        st.markdown(f"**当前状态:** {_status_text(selected_doc.analysis_status)}")

    with col2:
        _display_preview(project.id, selected_doc, width=300)

    st.divider()
    st.subheader("⚙️ 分析选项")

    _render_rule_kb_status(project)

    analysis_type = st.multiselect(
        "选择分析类型",
        ["尺寸标注识别", "材料清单提取", "施工步骤分析", "工程量计算", "合规性检查"],
        default=["尺寸标注识别", "材料清单提取"],
        key="drawing_analysis_types",
    )

    custom_prompt = st.text_area(
        "自定义分析指令（可选）",
        placeholder="请输入您希望AI特别关注的分析内容...",
        height=100,
        key="drawing_custom_prompt",
    )

    st.divider()
    col_btn1, col_btn2, col_btn3 = st.columns([1, 1, 3])

    with col_btn1:
        if st.button("🚀 开始分析", type="primary", width="stretch"):
            prompt = _format_drawing_prompt(selected_doc, analysis_type, custom_prompt)
            with st.spinner("正在预处理并调用智能问答 AI 分析图纸..."):
                result = _run_drawing_ai_job(
                    project=project,
                    doc=selected_doc,
                    provider=provider,
                    title=f"{ANALYSIS_SESSION_PREFIX}{selected_doc.filename}",
                    prompt=prompt,
                    job_kind="drawing_analysis",
                    extra_data={
                        "analysis_type": analysis_type,
                        "custom_prompt": custom_prompt,
                    },
                )

            if result.get("error"):
                st.error(f"分析失败: {result['error']}")
                st.stop()

            st.session_state["drawing_analysis_session_id"] = result["session_id"]
            duration_txt = _format_duration_seconds(result.get("duration_seconds"))
            duration_msg = f"，用时 {duration_txt}" if duration_txt else ""
            st.success(
                f"图纸分析完成{duration_msg}，共生成/使用 {result.get('image_count', 0)} 张图片；"
                "结果已保存，并已创建可继续追问的问答会话。"
            )
            st.rerun()

    with col_btn2:
        if st.button("✅ 图纸审核", type="secondary", width="stretch"):
            if drawing_rag.count_rules(project.id) == 0:
                st.error("审图规则知识库为空，请先在上方“审图规则知识库”中点击「同步规则到知识库」。")
                st.stop()

            with st.spinner("正在分诊图纸、检索相关规则与人工案例并审核..."):
                result = _run_drawing_review_rag(
                    project=project,
                    doc=selected_doc,
                    provider=provider,
                    custom_prompt=custom_prompt,
                )

            if result.get("error"):
                st.error(f"审核失败: {result['error']}")
                st.stop()

            st.session_state["drawing_analysis_session_id"] = result["session_id"]
            duration_txt = _format_duration_seconds(result.get("duration_seconds"))
            duration_msg = f"，用时 {duration_txt}" if duration_txt else ""
            st.success(
                f"图纸审核完成{duration_msg}，共生成/使用 {result.get('image_count', 0)} 张图片；"
                f"RAG 检索出 {result.get('rules_count', 0)} 条相关规则、"
                f"{result.get('cases_count', 0)} 条人工案例，结果已保存为可继续追问的会话。"
            )
            st.rerun()

    with col_btn3:
        if st.button("📋 查看历史分析", width="stretch"):
            st.session_state["drawing_analysis_tab"] = "history"
            st.rerun()

def view_drawing_analysis_chat(project: Project):
    """继续用问答方式分析图纸."""
    st.title("💬 图纸问答分析")
    st.caption(f"项目: {project.name}")

    sessions = _drawing_sessions(project.id)
    if not sessions:
        st.info("暂无图纸分析问答会话。请先在“新增分析”中分析一张图纸。")
        return

    col_session, col_provider = st.columns([2, 1])
    with col_session:
        current_id = st.session_state.get("drawing_analysis_session_id")
        default_idx = 0
        if current_id:
            for i, sess in enumerate(sessions):
                if sess.id == current_id:
                    default_idx = i
                    break
        selected_session = st.selectbox(
            "选择图纸分析会话",
            sessions,
            index=default_idx,
            format_func=lambda s: f"{s.title or '图纸分析'} · {format_beijing(s.updated_at, '%Y-%m-%d %H:%M')}",
            key="drawing_chat_session_selector",
        )
        st.session_state["drawing_analysis_session_id"] = selected_session.id

    with col_provider:
        providers = get_available_providers()
        default_provider = get_default_provider()
        provider = st.selectbox(
            "LLM 提供商",
            providers,
            index=providers.index(default_provider) if default_provider in providers else 0,
            key="drawing_chat_provider",
        )

    st.divider()
    _render_chat_session(selected_session.id)

    user_input = st.chat_input("继续追问这张图纸，例如：请列出需要复核的尺寸标注")
    if user_input:
        previous_images = []
        for msg in get_image_messages(selected_session.id):
            for img in msg.get("images", []):
                if img not in previous_images:
                    previous_images.append(img)

        with st.chat_message("user"):
            st.markdown(user_input)

        with st.chat_message("assistant"):
            with st.spinner("正在继续分析..."):
                result = vision_chat(
                    query=user_input,
                    image_paths=previous_images,
                    session_id=selected_session.id,
                    project_id=project.id,
                    user_id=_user_id(),
                    provider=provider,
                )
            if result.get("error"):
                st.error(f"错误: {result['error']}")
            else:
                st.markdown(result["answer"])
                st.rerun()


def view_drawing_analysis(project: Project | None):
    """图纸分析主页面（包含历史、新增、问答）."""
    if not project:
        st.warning("请先选择一个项目")
        return

    tab1, tab2, tab3 = st.tabs(["📋 历史分析", "🔍 新增分析", "💬 继续问答"])

    with tab1:
        view_drawing_analysis_history(project)

    with tab2:
        view_new_drawing_analysis(project)

    with tab3:
        view_drawing_analysis_chat(project)
