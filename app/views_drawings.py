"""图纸分析视图 - 查看历史图纸分析和新增图纸分析。"""
from __future__ import annotations
import html
import json
import re
from datetime import datetime
from pathlib import Path, PurePosixPath
from zipfile import ZipFile
import xml.etree.ElementTree as ET

import streamlit as st

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
from src.storage import documents_dir, project_dir


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
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
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


def _format_drawing_prompt(doc: Document, analysis_type: list[str], custom_prompt: str) -> str:
    """构建图纸分析提示词."""
    focus = "、".join(analysis_type) if analysis_type else "综合图纸分析"
    extra = f"\n\n用户补充要求：{custom_prompt}" if custom_prompt else ""
    return f"""
你是专业的建筑/施工图纸分析助手。请基于上传的图纸图片进行分析。

图纸文件：{doc.filename}
分析重点：{focus}

请输出结构化 Markdown，至少包含：
1. 图纸总体说明：判断图纸类型、主要空间/构件、可见图例或标题栏信息。
2. 尺寸与标注：列出能识别的关键尺寸、轴网、标高或编号；无法确认的请标注“需人工复核”。
3. 材料与工程量线索：提取材料、做法、设备/构件清单，以及可推断的数量/面积/长度线索。
4. 施工与质量关注点：列出施工顺序、关键节点、风险点、需要现场确认的问题。
5. 后续追问建议：给出 3-5 个用户可以继续问的问题。

要求：不要编造看不清的信息；不确定时明确说明；输出使用中文。{extra}
""".strip()


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


@st.cache_data(show_spinner=False)
def _load_review_rules(project_id: int) -> tuple[list[dict], str | None]:
    """读取项目审图规则库."""
    rules_path = Path("data") / "projects" / str(project_id) / RULES_WORKBOOK_NAME
    if not rules_path.exists():
        return [], f"未找到审图规则库：{rules_path}"

    try:
        rows = _read_xlsx_sheet_rows(rules_path, "审图规则库")
    except Exception as e:
        return [], f"读取审图规则库失败：{e}"

    header_idx = next((i for i, row in enumerate(rows) if "规则编号" in row), None)
    if header_idx is None:
        return [], "审图规则库中未找到表头“规则编号”"

    headers = rows[header_idx]
    rules = []
    for row in rows[header_idx + 1:]:
        if not any(row):
            continue
        item = {headers[i]: row[i] if i < len(row) else "" for i in range(len(headers)) if headers[i]}
        if item.get("规则编号"):
            rules.append(item)
    return rules, None


def _rules_to_markdown(rules: list[dict], max_rules: int = 120) -> str:
    """将规则库转换为适合放入提示词的Markdown."""
    lines = []
    for rule in rules[:max_rules]:
        lines.append(
            "- "
            f"[{rule.get('规则编号', '')}] "
            f"{rule.get('一级分类(审查大类)', '')} / {rule.get('二级分类', '')} / "
            f"{rule.get('审查项(审什么)', '')}\n"
            f"  - 审查内容：{rule.get('审查内容说明', '')}\n"
            f"  - 如何审核：{rule.get('如何审核(方法/步骤)', '')}\n"
            f"  - 判定标准：{rule.get('判定标准(命中即提疑)', '')}\n"
            f"  - 涉及专业/图纸：{rule.get('涉及专业', '')}；{rule.get('涉及图纸', '')}\n"
            f"  - AI判定逻辑：{rule.get('AI判定逻辑', '')}\n"
            f"  - 严重程度：{rule.get('严重程度', '')}；处理建议：{rule.get('处理建议', '')}"
        )
    if len(rules) > max_rules:
        lines.append(f"\n（规则库共 {len(rules)} 条，本次提示词纳入前 {max_rules} 条；如需全量审核请分批执行。）")
    return "\n".join(lines)


def _format_review_prompt(doc: Document, rules: list[dict], custom_prompt: str) -> str:
    """构建基于规则库的图纸审核提示词."""
    rules_md = _rules_to_markdown(rules)
    extra = f"\n\n用户补充要求：{custom_prompt}" if custom_prompt else ""
    return f"""
你是专业施工图/装饰图审图工程师。请严格依据下方《AI审图依据·审查规则库》对上传图纸进行审核。

图纸文件：{doc.filename}

审图规则库：
{rules_md}

请按规则逐条或按分类审核，并输出结构化 Markdown，格式如下：

## 审核结论
- 总体判断：通过 / 有疑问 / 不通过 / 信息不足
- 命中问题数量：高/中/低分别统计
- 信息不足但需人工复核的规则数量

## 标准提疑
命中问题必须用表格输出，且表头固定为：规则编号｜部位｜问题描述｜涉及图纸｜严重程度｜建议｜可信度。

字段要求：
- 规则编号：引用规则库中的规则编号。
- 部位：填写图纸中可识别的问题位置、房间、轴线、立面、节点、页码或区域；无法确认时写“图纸未明确，需人工定位”。
- 问题描述：说明命中的疑点、冲突或缺失内容，必须基于图纸可见信息。
- 涉及图纸：填写当前图纸名称，以及规则要求对比但当前缺失的图纸类型。
- 严重程度：使用规则库中的严重程度；无法确认时按“待复核”。
- 建议：引用或归纳规则库中的处理建议，说明下一步处理方式。
- 可信度：按“高 / 中 / 低”填写；图纸信息清晰且规则直接命中为高，需要跨图纸但当前图纸缺失为中，图纸模糊或只能推测为低。

## 未能判定/需补充图纸
列出由于缺少土建、机电、节点、大样、材料表等图纸导致无法判断的规则，并说明需要补充哪些图纸。

## 按专业汇总建议
按装饰、土建、机电、消防、结构等专业汇总下一步处理建议。

要求：
1. 只能基于当前图纸图片可见内容和规则库判断，不要编造看不清的信息。
2. 如果单张图纸无法完成跨专业比对，应标记为“信息不足/需补图”，不要直接判定通过。
3. 每条疑点必须引用规则编号，并严格使用“标准提疑”的七列格式：[规则编号|部位|问题描述|涉及图纸|严重程度|建议|可信度]。
4. 如果没有可确认命中的提疑，也要输出“标准提疑”表格，并在问题描述中写“未发现可确认提疑，需结合完整图纸人工复核”。
5. 输出中文。{extra}
""".strip()


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
    expected = ["规则编号", "部位", "问题描述", "涉及图纸", "严重程度", "建议", "可信度"]
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


def _render_document_result(doc: Document):
    """按类型渲染文档分析/审核结果."""
    analysis_data = _parse_analysis_data(doc)
    is_review = analysis_data.get("job_kind") == "drawing_review"

    if is_review:
        st.subheader("✅ 审核结果")
        review_items = _extract_standard_review_items(doc.analysis_summary or "")
        if review_items:
            _render_review_items_table(review_items)
            with st.expander("查看审核原文", expanded=False):
                st.write(doc.analysis_summary)
        else:
            st.info("未能从审核结果中解析出标准提疑表格，以下展示审核原文。")
            st.write(doc.analysis_summary)
    else:
        st.subheader("📝 分析摘要")
        st.write(doc.analysis_summary)

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


def _save_doc_analysis_result(doc_id: int, answer: str, data: dict):
    """保存图纸分析结果到 Document 分析字段."""
    with session() as s:
        doc_db = s.query(Document).filter(Document.id == doc_id).first()
        if doc_db:
            doc_db.analysis_status = "done"
            doc_db.analysis_summary = answer
            doc_db.analysis_data_json = json.dumps(data, ensure_ascii=False, indent=2)
            doc_db.analysis_error = None
            doc_db.analyzed_at = datetime.utcnow()
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
    with session() as s:
        doc_db = s.query(Document).filter(Document.id == doc.id).first()
        if doc_db:
            doc_db.analysis_status = "running"
            doc_db.analysis_error = None
            s.commit()

    image_paths, error = _prepare_drawing_images(doc, project.id)
    if error:
        _mark_doc_failed(doc.id, error)
        return {"error": error}

    result = vision_chat(
        query=prompt,
        image_paths=[str(p) for p in image_paths],
        session_id=None,
        project_id=project.id,
        user_id=_user_id(),
        provider=provider,
        title=title,
    )
    if result.get("error"):
        _mark_doc_failed(doc.id, result["error"])
        return result

    analysis_data = {
        "job_kind": job_kind,
        "provider": provider,
        "source_document_id": doc.id,
        "source_document": doc.filename,
        "preprocessed_images": [str(p) for p in image_paths],
        "chat_session_id": result["session_id"],
        "chat_message_id": result.get("message_id"),
    }
    if extra_data:
        analysis_data.update(extra_data)

    _save_doc_analysis_result(doc.id, result["answer"] or "", analysis_data)
    return {**result, "image_count": len(image_paths), "analysis_data": analysis_data}


def view_drawing_analysis_history(project: Project):
    """查看历史图纸分析页面."""
    st.title("📋 图纸分析历史")
    st.caption(f"项目: {project.name}")

    docs = _get_drawing_documents(project)
    sessions = _drawing_sessions(project.id)

    if not docs:
        st.info("暂无图纸文档。请先上传图纸文件。")
        return

    total = len(docs)
    analyzed = sum(1 for d in docs if d.analysis_status == "done")
    pending = sum(1 for d in docs if d.analysis_status in ["pending", "running"])
    failed = sum(1 for d in docs if d.analysis_status == "failed")

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("总图纸数", total)
    col2.metric("已分析", analyzed)
    col3.metric("待分析", pending)
    col4.metric("分析失败", failed)
    col5.metric("问答会话", len(sessions))

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
        ["全部", "已分析", "待分析", "分析失败"],
        key="drawing_history_status_filter",
    )

    filtered_docs = docs
    if status_filter == "已分析":
        filtered_docs = [d for d in docs if d.analysis_status == "done"]
    elif status_filter == "待分析":
        filtered_docs = [d for d in docs if d.analysis_status in ["pending", "running"]]
    elif status_filter == "分析失败":
        filtered_docs = [d for d in docs if d.analysis_status == "failed"]

    st.subheader(f"图纸列表 ({len(filtered_docs)})")

    for doc in filtered_docs:
        status_text = _status_text(doc.analysis_status)
        with st.expander(
            f"{_status_emoji(doc.analysis_status)} {doc.filename} · {status_text}",
            expanded=(doc.analysis_status == "done"),
        ):
            col_a, col_b = st.columns([3, 1])

            with col_a:
                upload_time = doc.uploaded_at.strftime("%Y-%m-%d %H:%M:%S") if doc.uploaded_at else "未知"
                st.markdown(f"**文件名:** {doc.filename}")
                st.markdown(f"**上传时间:** {upload_time}")
                st.markdown(f"**文件大小:** {doc.size_bytes / 1024:.1f} KB")
                st.markdown(f"**分析状态:** {status_text}")
                if doc.analyzed_at:
                    st.markdown(f"**分析时间:** {doc.analyzed_at.strftime('%Y-%m-%d %H:%M:%S')}")

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
            st.success("图纸已上传并选择。")

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
        upload_time = selected_doc.uploaded_at.strftime("%Y-%m-%d %H:%M:%S") if selected_doc.uploaded_at else "未知"
        st.markdown(f"**文件名:** {selected_doc.filename}")
        st.markdown(f"**上传时间:** {upload_time}")
        st.markdown(f"**文件大小:** {selected_doc.size_bytes / 1024:.1f} KB")
        st.markdown(f"**当前状态:** {_status_text(selected_doc.analysis_status)}")

    with col2:
        _display_preview(project.id, selected_doc, width=300)

    st.divider()
    st.subheader("⚙️ 分析选项")

    rules, rules_error = _load_review_rules(project.id)
    if rules_error:
        st.warning(rules_error)
    else:
        st.caption(f"✅ 已加载审图依据：data/projects/{project.id}/{RULES_WORKBOOK_NAME}，共 {len(rules)} 条规则。")

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
            st.success(
                f"图纸分析完成，共生成/使用 {result.get('image_count', 0)} 张图片；"
                "结果已保存，并已创建可继续追问的问答会话。"
            )
            st.rerun()

    with col_btn2:
        if st.button("✅ 图纸审核", type="secondary", width="stretch"):
            rules, rules_error = _load_review_rules(project.id)
            if rules_error:
                st.error(rules_error)
                st.stop()

            prompt = _format_review_prompt(selected_doc, rules, custom_prompt)
            with st.spinner("正在根据审图规则库预处理并审核图纸..."):
                result = _run_drawing_ai_job(
                    project=project,
                    doc=selected_doc,
                    provider=provider,
                    title=f"{REVIEW_SESSION_PREFIX}{selected_doc.filename}",
                    prompt=prompt,
                    job_kind="drawing_review",
                    extra_data={
                        "custom_prompt": custom_prompt,
                        "rules_workbook": RULES_WORKBOOK_NAME,
                        "rules_count": len(rules),
                    },
                )

            if result.get("error"):
                st.error(f"审核失败: {result['error']}")
                st.stop()

            st.session_state["drawing_analysis_session_id"] = result["session_id"]
            st.success(
                f"图纸审核完成，共生成/使用 {result.get('image_count', 0)} 张图片，"
                f"已依据 {len(rules)} 条规则生成审核结果，并保存为可继续追问的会话。"
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
            format_func=lambda s: f"{s.title or '图纸分析'} · {s.updated_at.strftime('%Y-%m-%d %H:%M')}",
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
