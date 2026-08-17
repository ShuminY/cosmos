"""图纸人工审核视图 - 只做人工批注，不调用 AI。

页面/逻辑与「图纸分析」相似：
- 支持选择已有图纸或上传新图纸
- 支持按页浏览、缩放/拖动查看原图
- 支持每页新增/编辑/删除人工批注
- 支持导出人工批注 Excel 和批注结果 PDF

但完全**不调用 AI**：没有规则库检索、没有视觉模型调用、没有标准提疑。
"""
from __future__ import annotations
import json
import sys
from datetime import datetime
from pathlib import Path

import streamlit as st
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import session, Document, Project
from src.time_utils import format_beijing, now_utc

# 复用图纸分析已有的原子能力（不再重复实现）
from views_drawings import (
    _save_uploaded_drawing,
    _extract_labels_after_upload,
    _get_drawing_documents,
    _prepare_drawing_images,
    _parse_analysis_data,
    _display_preview,
    _status_text,
    _load_manual_comments,
    _get_page_manual_comments,
    _render_manual_comments_ui,
    _render_full_resolution_image,
    _build_manual_comment_marker_image,
    _cached_marker_image,
    _annotated_image_output_path,
    _render_shape_annotator,
    _comment_has_shapes,
    _export_manual_comments_xlsx,
    _export_manual_comments_bundle,
    _export_comment_annotated_pdf,
    _ensure_page_labels,
    _load_page_labels,
    _format_page_label,
    CANVAS_AVAILABLE,
    st_canvas,
    _canvas_display_size,
    _bbox_to_canvas_rect,
    _normalize_manual_bbox,
)


MANUAL_JOB_KIND = "manual_review"


def _user_id() -> int | None:
    user = st.session_state.get("user") or {}
    return user.get("id")


# ============ 记录管理（避免误当成 AI 审核） ============
def _ensure_manual_review_record(doc_id: int):
    """确保文档带有 manual_review 元数据；不改写已有的 AI 审核/分析结果。"""
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
        # 已经是 AI 审核/分析结果的文档不覆盖 job_kind，仍允许在本页做人工批注
        if not data.get("job_kind"):
            data["job_kind"] = MANUAL_JOB_KIND
            doc_db.analysis_data_json = json.dumps(data, ensure_ascii=False, indent=2)
            if doc_db.analysis_status in ("pending", None):
                doc_db.analysis_status = "done"
            if doc_db.analyzed_at is None:
                doc_db.analyzed_at = now_utc()
            s.commit()


def _prepared_images_for(doc: Document, project_id: int) -> tuple[list[Path], str | None]:
    """人工审核也需要把 PDF 拆成 PNG 才能按页批注；
    如已由 AI 审核/分析生成过 preprocessed_images 就直接复用。"""
    data = _parse_analysis_data(doc)
    existing = [Path(p) for p in data.get("preprocessed_images", []) if Path(p).exists()]
    if existing:
        return existing, None

    image_paths, err = _prepare_drawing_images(doc, project_id)
    if err:
        return [], err

    # 把预处理后的图片路径记回 analysis_data_json，供导出 PDF 等复用
    with session() as s:
        doc_db = s.query(Document).filter(Document.id == doc.id).first()
        if doc_db:
            data_write = {}
            if doc_db.analysis_data_json:
                try:
                    data_write = json.loads(doc_db.analysis_data_json)
                except json.JSONDecodeError:
                    data_write = {}
            data_write["preprocessed_images"] = [str(p) for p in image_paths]
            data_write.setdefault("job_kind", MANUAL_JOB_KIND)
            doc_db.analysis_data_json = json.dumps(data_write, ensure_ascii=False, indent=2)
            s.commit()

    return image_paths, None


# ============ 页面：新增人工审核 ============
def _select_or_upload_doc(project: Project) -> Document | None:
    """新增页只做上传：不再提供“选择已上传图纸”入口，避免与历史页职责重叠."""
    st.caption("此页只负责上传新图纸；支持批量上传多个文件，上传后请到「人工审核列表」中进行批注和查看。")
    uploaded_files = st.file_uploader(
        "上传图纸文件",
        type=["pdf", "png", "jpg", "jpeg", "webp", "bmp", "dwg", "dxf"],
        accept_multiple_files=True,
        key="manual_review_upload_file",
        help="支持 PDF、图片及 CAD 图纸（DWG/DXF）。DWG 预览需系统安装 LibreOffice 或 ODA File Converter；DXF 可直接预览。",
    )
    if st.button("批量上传", type="primary", disabled=not uploaded_files):
        if not uploaded_files:
            st.warning("请先选择要上传的文件。")
            return None

        n_total = len(uploaded_files)
        n_ok = 0
        last_doc_id = None
        errors = []

        with st.spinner(f"正在批量保存 {n_total} 个图纸文件..."):
            for idx, uploaded_file in enumerate(uploaded_files, 1):
                try:
                    doc_id = _save_uploaded_drawing(project.id, uploaded_file)
                    # 记录为人工审核对象，让它出现在历史列表
                    _ensure_manual_review_record(doc_id)
                    # 只对最后一个文件做页面名称提取预处理，避免批量上传耗时太长
                    last_doc_id = doc_id
                    n_ok += 1
                except Exception as e:
                    errors.append(f"{uploaded_file.name}: {str(e)}")

        if last_doc_id:
            st.session_state["manual_review_uploaded_doc_id"] = last_doc_id
            # 让「历史」页默认打开最后一张新图
            st.session_state["manual_history_selected_doc_id"] = last_doc_id

            # 只对最后一个文件做自动提取，避免批量上传等待太久
            with st.spinner("正在对最后一张图纸预处理并自动提取页面名称（图名+图号）..."):
                named, err = _extract_labels_after_upload(project.id, last_doc_id)

            # 汇总结果
            if errors:
                st.error(f"批量上传完成：成功 {n_ok}/{n_total}，失败 {len(errors)}。")
                for err_msg in errors:
                    st.error(f"- {err_msg}")
            else:
                st.success(f"批量上传完成：成功 {n_ok}/{n_total}。已自动识别最后一张图纸的 {named} 页图名/图号。请到「人工审核列表」中继续批注。")
        else:
            st.error("所有文件上传均失败，请检查后重试。")

    selected_id = st.session_state.get("manual_review_uploaded_doc_id")
    if selected_id:
        docs = _get_drawing_documents(project)
        doc = next((d for d in docs if d.id == selected_id), None)
        if doc:
            return doc
    return None


def _render_page_navigation(doc_id: int, total_pages: int, labels: list[dict] | None = None,
                            page_comment_counts: list[int] | None = None) -> int:
    """页面导航（选择框 + 上一页/下一页）。返回 0-based selected_index.

    labels 提供时，选择框会显示“第 N 页 图名图号”。
    page_comment_counts 提供时，显示每页批注数量。
    """
    if total_pages <= 1:
        return 0

    labels = labels or []
    page_comment_counts = page_comment_counts or []
    page_key = f"manual_review_page_{doc_id}"
    current = st.session_state.get(page_key, 0)
    # 如果 Streamlit 保存了选项对象，提取 value
    if isinstance(current, dict) and "value" in current:
        current = int(current["value"])
    if not isinstance(current, int) or not (0 <= current < total_pages):
        current = 0

    # 提前构建所有选项标签，避免 format_func 闭包捕获变化导致 Streamlit 重置选择
    options = []
    for i in range(total_pages):
        suffix = _format_page_label(labels, i + 1)
        page_num = i + 1
        count = page_comment_counts[i] if i < len(page_comment_counts) else 0
        count_text = f"({count} 条批注)" if count > 0 else "(无批注)"
        if suffix:
            label = f"第 {page_num} 页 {suffix} · {count_text}"
        else:
            label = f"第 {page_num} 页 · {count_text}"
        options.append({"value": i, "label": label})

    col_prev, col_select, col_next = st.columns([1, 4, 1])
    with col_prev:
        if st.button("⬅️ 上一页", key=f"manual_prev_{doc_id}",
                     disabled=current <= 0, width="stretch"):
            st.session_state[page_key] = current - 1
            st.rerun()
    with col_next:
        if st.button("下一页 ➡️", key=f"manual_next_{doc_id}",
                     disabled=current >= total_pages - 1, width="stretch"):
            st.session_state[page_key] = current + 1
            st.rerun()
    with col_select:
        selected_value = st.selectbox(
            "选择页码",
            options=options,
            format_func=lambda opt: opt["label"],
            index=current,
            key=page_key,
        )
        selected = selected_value["value"]
    # 强制转换为 int，避免 Streamlit 返回非 int 类型导致下次验证失败被重置为 0
    return int(selected)


def _render_manual_review_workbench(doc: Document, project: Project):
    """人工审核核心工作区：左侧原图 + 右侧本页人工批注。"""
    _ensure_manual_review_record(doc.id)

    with st.spinner("正在预处理图纸..."):
        image_paths, err = _prepared_images_for(doc, project.id)
    if err:
        st.error(f"预处理失败：{err}")
        return
    if not image_paths:
        st.error("未生成可批注的图片。")
        return

    total_pages = len(image_paths)

    # 页面名称（图名+图号）：已缓存则直接读，否则提供一键提取
    data_now = _parse_analysis_data(doc)
    labels = _load_page_labels(data_now)
    has_labels = bool(labels) and len(labels) == total_pages

    col_lbl, col_btn = st.columns([3, 1])
    with col_lbl:
        if has_labels:
            named = sum(1 for x in labels if (x.get("title") or x.get("code")))
            st.caption(f"📑 已提取页面名称：{named}/{total_pages} 页有图名/图号。")
        else:
            st.caption("📑 尚未提取页面名称（图名+图号）。点击右侧按钮可从图纸右下角标题栏提取。")
    with col_btn:
        if st.button("🔤 提取页面名称", key=f"extract_labels_{doc.id}", width="stretch",
                     help="先从 PDF 文本层提取图号，再用视觉模型识别图名"):
            with st.spinner("正在提取页面名称（图号+图名）..."):
                labels = _ensure_page_labels(doc, project.id, image_paths, use_vision=True, force=True)
            st.success("页面名称已提取完成。")
            st.rerun()

    # 重新从 DB 读取最新的 analysis_data（含新增批注）
    with session() as s:
        fresh_doc = s.query(Document).filter(Document.id == doc.id).first()
    fresh_data = _parse_analysis_data(fresh_doc) if fresh_doc else {}

    # 计算每页批注数量用于导航显示
    page_comment_counts = []
    all_manual_comments = fresh_data.get("manual_comments", {}) if fresh_data else {}
    for p in range(1, total_pages + 1):
        comments = _get_page_manual_comments(fresh_data, p)
        page_comment_counts.append(len(comments))

    selected_index = _render_page_navigation(doc.id, total_pages, labels=labels if has_labels else None,
                                            page_comment_counts=page_comment_counts)
    page_number = selected_index + 1
    current_image = image_paths[selected_index]
    page_label = _format_page_label(labels, page_number) if has_labels else ""
    page_comments = _get_page_manual_comments(fresh_data, page_number)

    page_title = f"第 {page_number} 页{page_label}" if page_label else f"第 {page_number} 页"

    # 标注模式状态和已选位置
    annotate_mode_key = f"annotate_mode_{doc.id}_{page_number}"
    annotate_mode = st.session_state.get(annotate_mode_key, False)
    manual_bbox_key = f"manual_bbox_{doc.id}_{page_number}"
    manual_bbox_norm = st.session_state.get(manual_bbox_key, None)

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

    if CANVAS_AVAILABLE and annotate_mode:
        # ===== 标注模式：左侧 canvas + 右侧人工批注，批注栏保持在右侧不被挤到下方 =====
        col_canvas, col_comments = st.columns([7, 3])
        with col_canvas:
            st.caption(f"{page_title} / 共 {total_pages} 页")
            col_exit, col_hint = st.columns([1, 4])
            with col_exit:
                if st.button("❌ 退出标注", key=f"cancel_annotate_{doc.id}_{page_number}"):
                    st.session_state[annotate_mode_key] = False
                    st.session_state.pop(f"manual_shapes_{doc.id}_{page_number}", None)
                    st.rerun()
            with col_hint:
                st.caption("🔴 标注模式：选择工具（矩形/圆形/箭头/文字）后在下方图上标注问题位置（绿色为历史批注）。")

            _render_shape_annotator(doc, page_number, page_comments, current_image, canvas_max=1000)

        with col_comments:
            # 批注栏保持在右侧
            _render_manual_comments_ui(
                fresh_doc or doc, page_number, page_comments,
                page_image_path=current_image,
                manual_bbox_norm=manual_bbox_norm,
            )
    else:
        # ===== 普通模式：左图右批注两栏布局 =====
        col_image, col_comments = st.columns([6, 3])
        with col_image:
            st.caption(f"{page_title} / 共 {total_pages} 页")

            if not CANVAS_AVAILABLE:
                st.caption("❌ `streamlit-drawable-canvas` 未安装，请先执行：`pip install streamlit-drawable-canvas`")

            if manual_focus_bbox:
                st.caption(f"🔍 已定位到人工批注位置，图片自动放大居中；点查看器内“复位”可看整页。")

            # 人工批注位置：可勾选在图上显示/隐藏绿色标注图形（默认显示）
            has_comment_shapes = any(_comment_has_shapes(c) for c in page_comments)
            show_markers = True
            if has_comment_shapes:
                show_markers = st.checkbox(
                    "🟢 在图上显示人工批注标注",
                    value=True,
                    key=f"show_manual_markers_{doc.id}_{page_number}",
                )

            viewer_image = current_image
            manual_marked = 0
            if has_comment_shapes and show_markers:
                base_path = _annotated_image_output_path(doc.project_id, doc.id, current_image, selected_index)
                manual_marker_path = base_path.with_name(base_path.stem + "_人工.png")
                try:
                    viewer_image, manual_marked = _cached_marker_image(
                        current_image, page_comments, manual_marker_path,
                        _build_manual_comment_marker_image,
                    )
                except Exception:
                    viewer_image, manual_marked = current_image, 0

            # 稳定的 viewer key：仅在图片或焦点目标实际变化时才改变
            _focus_hash = hash(tuple(round(v, 4) for v in manual_focus_bbox)) if manual_focus_bbox else 0
            viewer_key = f"manual_review_image_{doc.id}_{selected_index}_{Path(viewer_image).stem}_{_focus_hash}"
            _render_full_resolution_image(
                viewer_image,
                f"{page_title}图纸",
                key=viewer_key,
                focus_bbox=manual_focus_bbox,
            )
            st.download_button(
                label="🖼️ 下载当前页图纸 PNG",
                data=current_image.read_bytes(),
                file_name=f"{Path(doc.filename).stem}_第{page_number:03d}页.png",
                mime="image/png",
                key=f"download_manual_page_{doc.id}_{selected_index}",
            )

        with col_comments:
            # 复用图纸分析里的批注 UI（增删改一致），并支持在图上拖拽画框选定位置
            _render_manual_comments_ui(
                fresh_doc or doc, page_number, page_comments,
                page_image_path=current_image,
                manual_bbox_norm=manual_bbox_norm,
            )


def view_new_manual_review(project: Project):
    """新增人工审核页面 - 只负责上传图纸文件。批注请到「人工审核列表」."""
    st.title("📝 新增人工审核")
    st.caption(f"项目: {project.name} · 本页只负责上传图纸，不调用 AI。")

    uploaded_doc = _select_or_upload_doc(project)

    if uploaded_doc is not None:
        st.divider()
        st.subheader("📄 图纸信息")
        col1, col2 = st.columns([1, 1])
        with col1:
            upload_time = format_beijing(uploaded_doc.uploaded_at, '%Y-%m-%d %H:%M:%S', fallback="未知")
            st.markdown(f"**文件名:** {uploaded_doc.filename}")
            st.markdown(f"**上传时间:** {upload_time}")
            st.markdown(f"**文件大小:** {uploaded_doc.size_bytes / 1024:.1f} KB")
            st.info("图纸已保存。请到「📋 人工审核列表」tab 中打开该图纸进行批注。")
        with col2:
            _display_preview(project.id, uploaded_doc, width=300)


# ============ 页面：人工审核列表 ============
def _manual_review_docs(project: Project) -> list[Document]:
    """筛选出至少有一条人工批注、或被标记为 manual_review 的文档。"""
    docs = _get_drawing_documents(project)
    result = []
    for d in docs:
        data = _parse_analysis_data(d)
        has_comments = bool(_load_manual_comments(data))
        is_manual = data.get("job_kind") == MANUAL_JOB_KIND
        if has_comments or is_manual:
            result.append(d)
    return result


def _doc_row_meta(doc: Document) -> dict:
    """为列表/筛选统一提取一行元数据."""
    data = _parse_analysis_data(doc)
    comments_by_page = _load_manual_comments(data)
    total_comments = sum(len(v) if isinstance(v, list) else 0 for v in comments_by_page.values())
    pages_with_comments = sum(1 for v in comments_by_page.values() if isinstance(v, list) and v)
    return {
        "job_kind": data.get("job_kind", ""),
        "total_comments": total_comments,
        "pages_with_comments": pages_with_comments,
        "analysis_data": data,
    }


def _render_manual_review_filters(docs: list[Document]) -> list[Document]:
    """筛选栏；返回过滤后的文档列表。"""
    st.markdown("#### 🔎 筛选")
    row1 = st.columns([2, 1, 1, 1])
    keyword = row1[0].text_input(
        "文件名 / 关键字",
        key="manual_list_filter_kw",
        placeholder="按文件名过滤",
    ).strip().lower()
    kind_choice = row1[1].selectbox(
        "审核类型",
        ["全部", "纯人工", "兼含 AI"],
        key="manual_list_filter_kind",
    )
    comment_choice = row1[2].selectbox(
        "批注情况",
        ["全部", "有批注", "无批注"],
        key="manual_list_filter_comments",
    )
    sort_choice = row1[3].selectbox(
        "排序",
        ["最近上传", "最早上传", "批注最多", "批注最少", "文件名"],
        key="manual_list_filter_sort",
    )

    # 应用过滤
    rows = [(d, _doc_row_meta(d)) for d in docs]

    if keyword:
        rows = [
            (d, m) for d, m in rows
            if keyword in (d.filename or "").lower()
        ]
    if kind_choice == "纯人工":
        rows = [(d, m) for d, m in rows if m["job_kind"] == MANUAL_JOB_KIND]
    elif kind_choice == "兼含 AI":
        rows = [(d, m) for d, m in rows if m["job_kind"] != MANUAL_JOB_KIND]
    if comment_choice == "有批注":
        rows = [(d, m) for d, m in rows if m["total_comments"] > 0]
    elif comment_choice == "无批注":
        rows = [(d, m) for d, m in rows if m["total_comments"] == 0]

    # 排序
    if sort_choice == "最近上传":
        rows.sort(key=lambda p: (p[0].uploaded_at or datetime.min), reverse=True)
    elif sort_choice == "最早上传":
        rows.sort(key=lambda p: (p[0].uploaded_at or datetime.min))
    elif sort_choice == "批注最多":
        rows.sort(key=lambda p: p[1]["total_comments"], reverse=True)
    elif sort_choice == "批注最少":
        rows.sort(key=lambda p: p[1]["total_comments"])
    elif sort_choice == "文件名":
        rows.sort(key=lambda p: (p[0].filename or "").lower())

    st.caption(f"共 {len(rows)} 条记录（过滤后）")
    # 把 meta 缓存到 session 让下方渲染复用，避免重复解析 JSON
    st.session_state["_manual_list_rows_meta"] = {d.id: m for d, m in rows}
    return [d for d, _ in rows]


def _render_manual_review_stats(all_docs: list[Document], filtered_docs: list[Document]):
    """在筛选和列表之间显示统计数量."""
    meta_by_id: dict = st.session_state.get("_manual_list_rows_meta", {})

    def _stats(rows: list[Document]) -> tuple[int, int, int, int, int]:
        total = len(rows)
        manual = ai_mixed = with_comments = comment_total = 0
        for d in rows:
            m = meta_by_id.get(d.id) or _doc_row_meta(d)
            if m["job_kind"] == MANUAL_JOB_KIND:
                manual += 1
            else:
                ai_mixed += 1
            if m["total_comments"] > 0:
                with_comments += 1
            comment_total += m["total_comments"]
        return total, manual, ai_mixed, with_comments, comment_total

    f_total, f_manual, f_ai, f_with, f_cmt = _stats(filtered_docs)
    a_total, _, _, _, _ = _stats(all_docs)

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("当前筛选记录", f_total, delta=f"共 {a_total}" if a_total != f_total else None)
    c2.metric("纯人工", f_manual)
    c3.metric("兼含 AI", f_ai)
    c4.metric("有批注", f_with)
    c5.metric("批注总数", f_cmt)


def _paginate(docs: list[Document], page_size_key: str, page_key: str) -> tuple[list[Document], int, int, int]:
    """对文档列表分页；返回 (当前页数据, 当前页码 1-based, 总页数, 页大小)."""
    size_options = [10, 20, 50, 100]
    c_size, c_page = st.columns([1, 3])
    with c_size:
        page_size = st.selectbox(
            "每页显示",
            size_options,
            index=size_options.index(st.session_state.get(page_size_key, 10))
            if st.session_state.get(page_size_key, 10) in size_options else 0,
            key=page_size_key,
        )

    total_pages = max(1, (len(docs) + page_size - 1) // page_size)
    # 当前页可能因筛选/页大小变化越界，做一次夹紧
    current = int(st.session_state.get(page_key, 1) or 1)
    current = max(1, min(current, total_pages))
    st.session_state[page_key] = current

    with c_page:
        cp_prev, cp_info, cp_next = st.columns([1, 3, 1])
        with cp_prev:
            if st.button("⬅️ 上一页", key=f"{page_key}_prev", disabled=current <= 1, width="stretch"):
                st.session_state[page_key] = current - 1
                st.rerun()
        with cp_next:
            if st.button("下一页 ➡️", key=f"{page_key}_next", disabled=current >= total_pages, width="stretch"):
                st.session_state[page_key] = current + 1
                st.rerun()
        with cp_info:
            # 让用户也能直接跳页
            jumped = st.number_input(
                f"第 {current} / {total_pages} 页 · 跳转",
                min_value=1,
                max_value=total_pages,
                value=current,
                step=1,
                key=f"{page_key}_jump",
                label_visibility="visible",
            )
            if jumped != current:
                st.session_state[page_key] = int(jumped)
                st.rerun()

    start = (current - 1) * page_size
    end = start + page_size
    return docs[start:end], current, total_pages, page_size


def _render_manual_review_list(docs: list[Document]) -> Document | None:
    """把过滤后的文档展示为紧凑列表：一行一张图，点击「打开」进入工作区。已带分页."""
    if not docs:
        st.info("没有符合筛选条件的记录。")
        return None

    page_docs, cur_page, total_pages, page_size = _paginate(
        docs, page_size_key="manual_list_page_size", page_key="manual_list_page"
    )

    meta_by_id: dict = st.session_state.get("_manual_list_rows_meta", {})
    session_selected = st.session_state.get("manual_history_selected_doc_id")

    for doc in page_docs:
        m = meta_by_id.get(doc.id) or _doc_row_meta(doc)
        badge = "📝 人工" if m["job_kind"] == MANUAL_JOB_KIND else "🔀 兼含 AI"
        upload_str = format_beijing(doc.uploaded_at, "%Y-%m-%d %H:%M", fallback="—")
        is_current = doc.id == session_selected

        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([3, 1.2, 1, 1.2])
            c1.markdown(f"**{doc.filename}**")
            c1.caption(f"{badge} · 上传于 {upload_str}")
            c2.metric("批注", m["total_comments"])
            c3.metric("涉及页", m["pages_with_comments"])
            with c4:
                if st.button("📝 打开", key=f"open_manual_doc_{doc.id}", width="stretch", type="primary"):
                    st.session_state["manual_history_selected_doc_id"] = doc.id
                    st.rerun()

    # 页脚再摆一行分页信息，方便长列表底部翻页
    st.caption(f"当前第 {cur_page} / {total_pages} 页 · 每页 {page_size} 条 · 本页 {len(page_docs)} 条")

    if session_selected:
        return next((d for d in docs if d.id == session_selected), None)
    return None


def view_manual_review_history(project: Project):
    """人工审核列表页面：筛选 → 打开某张图 → 完整批注工作区 + 导出.

    打开某条记录后自动进入「专注模式」，隐藏列表，只展示工作区 + 返回按钮。
    """
    st.title("📋 人工审核列表")
    st.caption(f"项目: {project.name} · 在此页浏览、筛选、批注、导出。")

    docs = _manual_review_docs(project)
    if not docs:
        st.info("暂无人工审核记录。请先到「新增人工审核」上传图纸。")
        return

    session_selected = st.session_state.get("manual_history_selected_doc_id")
    focus_doc: Document | None = None
    if session_selected is not None:
        focus_doc = next((d for d in docs if d.id == session_selected), None)

    # ============ 专注模式：打开了某条记录 → 隐藏列表 ============
    if focus_doc is not None:
        _render_manual_review_focus(focus_doc, project)
        return

    # ============ 列表模式 ============
    filtered_docs = _render_manual_review_filters(docs)

    st.divider()
    _render_manual_review_stats(docs, filtered_docs)

    st.divider()
    st.markdown("#### 📄 记录列表")
    _render_manual_review_list(filtered_docs)


def _render_manual_review_focus(doc: Document, project: Project):
    """专注模式：只展示单条记录的批注工作区 + 导出，隐藏其他记录."""
    # 顶部返回条
    col_back, col_title = st.columns([1, 5])
    with col_back:
        if st.button("⬅️ 返回列表", key="manual_focus_back", width="stretch"):
            st.session_state.pop("manual_history_selected_doc_id", None)
            st.rerun()
    with col_title:
        st.caption("已进入单条记录批注模式，其他记录已隐藏。点击左侧返回可回到列表。")

    data = _parse_analysis_data(doc)
    comments_by_page = _load_manual_comments(data)
    total_comments = sum(len(v) if isinstance(v, list) else 0 for v in comments_by_page.values())
    pages_with_comments = sum(1 for v in comments_by_page.values() if isinstance(v, list) and v)
    job_kind = data.get("job_kind", "")
    badge = "📝 人工审核" if job_kind == MANUAL_JOB_KIND else "🔀 兼含 AI 审核"

    st.divider()
    col_a, col_b = st.columns([3, 1])
    with col_a:
        st.markdown(f"**{badge} · {doc.filename}**")
        st.markdown(f"**上传时间:** {format_beijing(doc.uploaded_at, '%Y-%m-%d %H:%M:%S', fallback='未知')}")
        if doc.analyzed_at:
            st.markdown(f"**最近记录时间:** {format_beijing(doc.analyzed_at, '%Y-%m-%d %H:%M:%S')}")
        st.markdown(f"**批注总数:** {total_comments}（分布在 {pages_with_comments} 页）")
    with col_b:
        _display_preview(project.id, doc, width=200)

    st.divider()
    st.subheader("🧑‍⚖️ 人工审核工作区")
    _render_manual_review_workbench(doc, project)

    # 导出
    st.divider()
    st.subheader("⬇️ 导出")
    timestamp = format_beijing(doc.analyzed_at, '%Y%m%d_%H%M%S') if doc.analyzed_at else "export"
    col_e1, col_e2 = st.columns(2)
    with col_e1:
        bundle_bytes, bundle_ext, bundle_mime = _export_manual_comments_bundle(doc, data)
        _is_zip = bundle_ext == "zip"
        st.download_button(
            label="📝 导出人工批注（含附件）" if _is_zip else "📝 导出人工批注 Excel",
            data=bundle_bytes or b"",
            file_name=f"{Path(doc.filename).stem}_人工批注_{timestamp}.{bundle_ext or 'xlsx'}",
            mime=bundle_mime or "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"manual_export_xlsx_{doc.id}",
            disabled=bundle_bytes is None,
            help="含文件附件时打包为 ZIP（Excel + 附件文件）" if _is_zip else (None if bundle_bytes else "暂无人工批注可导出"),
        )
    with col_e2:
        if st.button(
            "📑 导出批注结果 PDF",
            key=f"manual_gen_pdf_{doc.id}",
            help="将每页图纸与人工批注拼接，合成一个多页 PDF",
        ):
            with st.spinner("正在生成批注结果 PDF..."):
                st.session_state[f"manual_pdf_{doc.id}"] = _export_comment_annotated_pdf(doc, data)
        pdf_bytes = st.session_state.get(f"manual_pdf_{doc.id}")
        if pdf_bytes:
            st.download_button(
                label="⬇️ 下载批注结果 PDF",
                data=pdf_bytes,
                file_name=f"{Path(doc.filename).stem}_人工审核批注_{timestamp}.pdf",
                mime="application/pdf",
                key=f"manual_download_pdf_{doc.id}",
            )


def _default_selector_index(project: Project, target_doc_id: int) -> int:
    """在文档列表里定位 target_doc_id 的位置，找不到返回 0."""
    docs = _get_drawing_documents(project)
    for i, d in enumerate(docs):
        if d.id == target_doc_id:
            return i
    return 0


# ============ 主入口 ============
def view_manual_review(project: Project | None):
    """人工审核主页面（历史 + 新增）."""
    if not project:
        st.warning("请先选择一个项目")
        return

    tab_history, tab_new = st.tabs(["📋 人工审核列表", "🔍 新增人工审核"])
    with tab_history:
        view_manual_review_history(project)
    with tab_new:
        view_new_manual_review(project)
