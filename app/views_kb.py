"""Streamlit views for Knowledge Base and Chatbot."""
from __future__ import annotations
import sys
from pathlib import Path

import streamlit as st
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import session, Document, Project, init_db
from src.kb import kb_status, index_document, index_project_documents, search_kb
from src.chatbot import (
    chat, list_chat_sessions, get_chat_messages, delete_chat_session,
    get_available_providers, get_default_provider, set_default_provider,
    load_provider_config, save_provider_config,
)

init_db()


def _user_id() -> int:
    """Get current user ID from session state."""
    return st.session_state["user"]["id"]


def _get_visible_projects() -> list[Project]:
    """Get projects visible to current user."""
    from views import visible_projects
    return visible_projects(_user_id())


# ============ Knowledge Base Status View ============
def view_knowledge_base():
    """Knowledge Base status and management page."""
    st.title("📚 知识库")
    st.caption("管理项目文档的向量索引，用于智能问答")

    projects = _get_visible_projects()
    if not projects:
        st.info("您还没有任何项目。请先创建一个项目并上传文档。")
        return

    # Project selector
    project_names = [p.name for p in projects]
    project_idx = st.selectbox("选择项目", range(len(projects)),
                               format_func=lambda i: project_names[i],
                               key="kb_project_selector")
    project = projects[project_idx]

    # Status overview
    status = kb_status(project.id)
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("文档总数", status["total_docs"])
    col2.metric("已索引", status["indexed"])
    col3.metric("待索引", status["pending"])
    col4.metric("索引失败", status["failed"])
    col5.metric("文本块总数", status["total_chunks"])

    st.divider()

    # Document list with KB status
    with session() as s:
        docs = s.execute(
            select(Document).where(Document.project_id == project.id)
        ).scalars().all()

    if not docs:
        st.info("该项目还没有上传任何文档。请先在项目详情页上传文档。")
        return

    st.subheader("文档列表")

    # Bulk actions
    col_a, col_b = st.columns([1, 3])
    with col_a:
        if st.button("🔄 索引所有待处理文档", type="primary"):
            with st.spinner("正在建立索引..."):
                success, failed, errors = index_project_documents(project.id)
                if errors:
                    for err in errors:
                        st.error(err)
                st.success(f"索引完成：成功 {success} 个，失败 {failed} 个")
                st.rerun()

    # Document table
    rows = []
    for doc in docs:
        kb_color = {
            "pending": "⚪",
            "indexing": "🟡",
            "indexed": "✅",
            "failed": "❌",
        }.get(doc.kb_status, "?")
        rows.append({
            "状态": kb_color,
            "文件名": doc.filename,
            "分类": doc.category,
            "KB状态": doc.kb_status,
            "文本块数": str(doc.kb_chunk_count) if doc.kb_chunk_count else "-",
            "KB索引时间": doc.kb_indexed_at.strftime("%Y-%m-%d %H:%M") if doc.kb_indexed_at else "-",
            "KB错误": doc.kb_error or "-",
            "id": doc.id,
        })

    st.dataframe(
        rows,
        column_config={
            "id": None,  # Hide
        },
        width="stretch",
    )

    # Per-document reindex action
    st.subheader("重新索引单个文档")
    doc_by_name = {d.filename: d for d in docs}
    selected_doc_name = st.selectbox("选择文档", [""] + list(doc_by_name.keys()))
    if selected_doc_name:
        doc = doc_by_name[selected_doc_name]
        if st.button(f"重新索引: {doc.filename}"):
            with st.spinner("正在建立索引..."):
                success, error = index_document(doc.id)
                if success:
                    st.success("索引成功！")
                else:
                    st.error(f"索引失败：{error}")
                st.rerun()


# ============ LLM Settings View ============
def view_llm_settings():
    """LLM provider configuration page."""
    st.title("⚙️ LLM 设置")
    st.caption("配置用于聊天机器人的大语言模型提供商")

    providers = get_available_providers()
    current_default = get_default_provider()

    st.subheader("默认提供商")
    new_default = st.selectbox(
        "选择默认 LLM 提供商",
        providers,
        index=providers.index(current_default) if current_default in providers else 0,
    )
    if new_default != current_default:
        set_default_provider(new_default)
        st.success(f"已设置默认提供商为: {new_default}")
        st.rerun()

    st.divider()
    st.subheader("提供商配置")

    for provider in providers:
        with st.expander(f"{provider.upper()} - 配置", expanded=(provider == current_default)):
            config = load_provider_config(provider)

            if "api_key" in config:
                config["api_key"] = st.text_input(
                    "API Key",
                    value=config.get("api_key", ""),
                    type="password",
                    key=f"{provider}_api_key",
                )
            if "secret_key" in config:
                config["secret_key"] = st.text_input(
                    "Secret Key",
                    value=config.get("secret_key", ""),
                    type="password",
                    key=f"{provider}_secret_key",
                )
            if "base_url" in config:
                config["base_url"] = st.text_input(
                    "Base URL",
                    value=config.get("base_url", ""),
                    key=f"{provider}_base_url",
                )
            if "model" in config:
                config["model"] = st.text_input(
                    "Model",
                    value=config.get("model", ""),
                    key=f"{provider}_model",
                )

            if st.button("保存配置", key=f"save_{provider}"):
                save_provider_config(provider, config)
                st.success("配置已保存！")


# ============ Chatbot View ============
def view_chatbot():
    """Chatbot interface with conversation history."""
    st.title("💬 智能问答")
    st.caption("基于项目知识库的智能问答")

    projects = _get_visible_projects()
    if not projects:
        st.info("您还没有任何项目。请先创建一个项目并上传文档。")
        return

    # Top bar: project selector and provider selector
    col1, col2, col3 = st.columns([3, 3, 2])
    with col1:
        project_names = [p.name for p in projects]
        project_idx = st.selectbox(
            "选择项目",
            range(len(projects)),
            format_func=lambda i: project_names[i],
            key="chat_project_selector",
        )
        project = projects[project_idx]

    with col2:
        # Provider selector
        providers = get_available_providers()
        default_provider = get_default_provider()
        provider = st.selectbox(
            "LLM 提供商",
            providers,
            index=providers.index(default_provider) if default_provider in providers else 0,
            key="chat_provider_selector",
        )

    with col3:
        if st.button("➕ 新对话", type="primary", width="stretch"):
            st.session_state.pop("current_chat_session", None)
            st.rerun()

    # Session history sidebar (use expander instead)
    sessions = list_chat_sessions(project_id=project.id, user_id=_user_id())
    if sessions:
        with st.expander("📜 对话历史", expanded=False):
            for sess in sessions:
                col1, col2 = st.columns([4, 1])
                with col1:
                    if st.button(sess.title or "新对话", key=f"sess_{sess.id}", width="stretch"):
                        st.session_state["current_chat_session"] = sess.id
                        st.rerun()
                with col2:
                    if st.button("🗑️", key=f"del_{sess.id}", help="删除对话"):
                        delete_chat_session(sess.id)
                        if st.session_state.get("current_chat_session") == sess.id:
                            st.session_state.pop("current_chat_session", None)
                        st.rerun()

    st.divider()

    # Main chat area
    session_id = st.session_state.get("current_chat_session")
    messages = []
    if session_id:
        messages = get_chat_messages(session_id)

    # Display messages
    for msg in messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

            # Show context sources for assistant messages
            if msg["role"] == "assistant" and msg.get("context_chunk_ids"):
                with st.expander("📄 参考来源"):
                    # Load chunk details from DB
                    with session() as s:
                        from src.db import DocumentChunk
                        for cid in msg["context_chunk_ids"]:
                            chunk = s.get(DocumentChunk, cid)
                            if chunk:
                                st.caption(f"**{chunk.document.filename}**")
                                st.text(chunk.text[:200] + "..." if len(chunk.text) > 200 else chunk.text)

    # Chat input
    user_input = st.chat_input("提问...")
    if user_input:
        # Display user message immediately
        with st.chat_message("user"):
            st.markdown(user_input)

        # Generate response
        with st.chat_message("assistant"):
            with st.spinner("正在检索知识库并生成回答..."):
                result = chat(
                    project_id=project.id,
                    query=user_input,
                    session_id=session_id,
                    user_id=_user_id(),
                    provider=provider,
                )

            if result.get("error"):
                st.error(f"错误: {result['error']}")
            else:
                st.markdown(result["answer"])

                # Show context sources
                with st.expander("📄 参考来源"):
                    for ctx in result["context_chunks"]:
                        score_pct = int(ctx["score"] * 100)
                        st.caption(f"**{ctx['filename']}** - 相似度 {score_pct}%")
                        st.text(ctx["text"][:200] + "..." if len(ctx["text"]) > 200 else ctx["text"])

                # Update session in state if it was newly created
                if "current_chat_session" not in st.session_state or st.session_state["current_chat_session"] != result["session_id"]:
                    st.session_state["current_chat_session"] = result["session_id"]
                    st.rerun()
