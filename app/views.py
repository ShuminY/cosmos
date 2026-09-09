"""New views for the multi-tenant phase: login, projects, documents, admin.

Imported by streamlit_app.py — each function renders one page.
"""
from __future__ import annotations
import json
import secrets
import shutil
import sys
import time
from pathlib import Path

import streamlit as st
from sqlalchemy import select

# Make src/ importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import (
    session, init_db, User, Project, ProjectMember, Capture, Material,
    Document, Analysis, DOCUMENT_CATEGORIES, DOCUMENT_CATEGORY_KEYS, MATERIAL_TYPES,
)
from src.auth import authenticate, create_user, update_password, hash_password, ensure_default_admin
from src.storage import documents_dir, project_dir, materials_dir, material_dir, material_slug_dir, slugify, capture_dir, jobs_dir
from src.analyzers import run_analyzer, serialize
from src.settings import get_setting, set_setting
from src.time_utils import beijing_timestamp, format_beijing, now_beijing, now_utc


# ============ session helpers ============
def get_current_project() -> Project | None:
    """从session获取当前选中的项目."""
    pid = st.session_state.get("current_project_id")
    if not pid:
        return None
    with session() as s:
        return s.get(Project, pid)


def current_user() -> dict | None:
    return st.session_state.get("user")


# ============ 登录保持（刷新不掉线） ============
# 登录成功后签发一个 token 写进 URL query param（?token=...），刷新页面时
# URL 不变，凭 token 自动恢复登录态。token 存 settings.json，7 天过期。
_AUTH_TOKEN_TTL_SECONDS = 7 * 86400


def _auth_tokens() -> dict:
    """读取 token 表并惰性清理过期项。格式 {token: {"user_id": int, "expires": ts}}"""
    tokens = get_setting("auth_tokens", {}) or {}
    if not isinstance(tokens, dict):
        return {}
    now = time.time()
    valid = {t: v for t, v in tokens.items()
             if isinstance(v, dict) and v.get("expires", 0) > now}
    if len(valid) != len(tokens):
        set_setting("auth_tokens", valid)
    return valid


def _issue_auth_token(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    tokens = _auth_tokens()
    tokens[token] = {"user_id": user_id,
                     "expires": time.time() + _AUTH_TOKEN_TTL_SECONDS}
    set_setting("auth_tokens", tokens)
    return token


def _resolve_auth_token(token: str) -> dict | None:
    info = _auth_tokens().get(token)
    if not info:
        return None
    with session() as s:
        u = s.get(User, info.get("user_id"))
        if not u or not u.is_active:
            return None
        return {"id": u.id, "email": u.email, "name": u.name, "role": u.role}


def _revoke_auth_token(token: str):
    tokens = _auth_tokens()
    if token in tokens:
        del tokens[token]
        set_setting("auth_tokens", tokens)


def _try_token_login():
    """URL 里有 token 且有效则恢复登录态；无效/过期则从 URL 清掉。"""
    token = st.query_params.get("token")
    if not token:
        return
    user = _resolve_auth_token(token)
    if user:
        st.session_state["user"] = user
    else:
        del st.query_params["token"]


def require_login():
    if not current_user():
        _try_token_login()
    if not current_user():
        view_login()
        st.stop()


def is_admin() -> bool:
    u = current_user()
    return bool(u and u.get("role") == "admin")


def visible_projects(user_id: int) -> list[Project]:
    """Projects this user owns or is a member of."""
    with session() as s:
        owned = s.execute(select(Project).where(Project.owner_id == user_id)).scalars().all()
        memberships = s.execute(
            select(Project).join(ProjectMember,
                                 ProjectMember.project_id == Project.id)
            .where(ProjectMember.user_id == user_id)
        ).scalars().all()
        seen = {}
        for p in owned + memberships:
            seen.setdefault(p.id, p)
        out = sorted(seen.values(), key=lambda p: p.created_at, reverse=True)
        for p in out:
            s.expunge(p)
        return out


# ============ Login / Register / Profile ============
def view_login():
    init_db()
    # Make sure a default admin exists
    admin, created = ensure_default_admin()

    # If user clicked "Sign up", show that flow instead
    if st.session_state.get("auth_mode") == "register":
        view_register()
        return

    st.title("Sign in")
    st.caption("Cosmos Construction-Progress POC")
    if created:
        st.info("Default admin created: **admin@cosmos.local** / **admin**. Please change the password after first login.")

    with st.form("login_form"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        submit = st.form_submit_button("Log in", type="primary")
    if submit:
        u = authenticate(email, password)
        if not u:
            st.error("Wrong email or password — or account is deactivated.")
        else:
            st.session_state["user"] = {
                "id": u.id, "email": u.email, "name": u.name, "role": u.role,
            }
            # 签发保持登录 token（刷新页面凭 URL 里的 token 自动登录）
            st.query_params["token"] = _issue_auth_token(u.id)
            st.session_state.pop("auth_mode", None)
            # 只有一个可见项目时，登录后自动选中，省去手动切换
            projects = visible_projects(u.id)
            if len(projects) == 1:
                st.session_state["current_project_id"] = projects[0].id
            st.rerun()

    if get_setting("registration_open", True):
        st.divider()
        st.caption("Don't have an account?")
        if st.button("Sign up"):
            st.session_state["auth_mode"] = "register"
            st.rerun()
    else:
        st.caption("Registration is currently closed. Ask an admin to create an account for you.")


def view_register():
    st.title("Sign up")
    st.caption("Create a new normal-user account.")
    if not get_setting("registration_open", True):
        st.error("Registration is currently closed. Ask an admin for an invite.")
        if st.button("Back to sign in"):
            st.session_state.pop("auth_mode", None)
            st.rerun()
        return

    min_len = int(get_setting("min_password_length", 8))
    with st.form("register_form"):
        name = st.text_input("Full name")
        email = st.text_input("Email")
        password = st.text_input(f"Password (min {min_len} chars)", type="password")
        password2 = st.text_input("Confirm password", type="password")
        submit = st.form_submit_button("Create account", type="primary")
    if submit:
        email_n = email.strip().lower()
        if not name.strip():
            st.error("Name required.")
            return
        if "@" not in email_n or len(email_n) < 5:
            st.error("Enter a valid email.")
            return
        if password != password2:
            st.error("Passwords don't match.")
            return
        if len(password) < min_len:
            st.error(f"Password too short (min {min_len} chars).")
            return
        # Check unique
        with session() as s:
            existing = s.execute(select(User).where(User.email == email_n)).scalar_one_or_none()
        if existing:
            st.error("An account with that email already exists.")
            return
        try:
            u = create_user(email_n, name.strip(), password, role="user")
        except Exception as e:
            st.error(f"Failed to create account: {e}")
            return
        # Auto-login on success
        st.session_state["user"] = {"id": u.id, "email": u.email, "name": u.name, "role": u.role}
        st.query_params["token"] = _issue_auth_token(u.id)
        st.session_state.pop("auth_mode", None)
        st.success(f"Welcome, {u.name}! You're signed in.")
        time.sleep(0.5)
        st.rerun()

    if st.button("Back to sign in"):
        st.session_state.pop("auth_mode", None)
        st.rerun()


def view_profile():
    st.title("Profile")
    u = current_user()
    st.markdown(f"`{u['email']}` · role: **{u['role']}**")
    st.divider()

    st.subheader("Edit profile")
    with st.form("edit_profile"):
        new_name = st.text_input("Name", value=u["name"])
        save_name = st.form_submit_button("Save name")
    if save_name and new_name.strip() and new_name != u["name"]:
        with session() as s:
            uu = s.get(User, u["id"])
            uu.name = new_name.strip()
            s.add(uu)
        st.session_state["user"]["name"] = new_name.strip()
        st.success("Name updated.")
        st.rerun()

    st.divider()
    st.subheader("Change password")
    min_len = int(get_setting("min_password_length", 8))
    with st.form("change_pw"):
        old = st.text_input("Current password", type="password")
        new = st.text_input(f"New password (min {min_len} chars)", type="password")
        new2 = st.text_input("Confirm new password", type="password")
        submit = st.form_submit_button("Change password")
    if submit:
        if not authenticate(u["email"], old):
            st.error("Current password incorrect.")
        elif new != new2:
            st.error("New passwords don't match.")
        elif len(new) < min_len:
            st.error(f"Password too short (min {min_len} chars).")
        elif old == new:
            st.error("New password must differ from current.")
        else:
            update_password(u["id"], new)
            st.success("Password changed.")

    st.divider()
    if st.button("Log out", type="secondary"):
        token = st.query_params.get("token")
        if token:
            _revoke_auth_token(token)
            del st.query_params["token"]
        for k in ("user", "current_project_id"):
            st.session_state.pop(k, None)
        st.rerun()


# ============ Admin · Users ============
def view_admin_users():
    if not is_admin():
        st.error("Admin only.")
        return
    st.title("Admin · Users")

    with st.expander("Site settings", expanded=False):
        cur_reg = bool(get_setting("registration_open", True))
        new_reg = st.toggle("Self-service registration is OPEN", value=cur_reg,
                            help="When off, only admins can create new users.")
        cur_min = int(get_setting("min_password_length", 8))
        new_min = st.slider("Minimum password length", 4, 32, cur_min)
        if new_reg != cur_reg or new_min != cur_min:
            if st.button("Save site settings"):
                set_setting("registration_open", new_reg)
                set_setting("min_password_length", new_min)
                st.success("Saved.")
                st.rerun()
    with session() as s:
        users = s.execute(select(User).order_by(User.id)).scalars().all()
        rows = [{"id": u.id, "email": u.email, "name": u.name, "role": u.role,
                 "active": u.is_active,
                 "created": format_beijing(u.created_at),
                 "last_login": format_beijing(u.last_login_at, fallback="—")}
                for u in users]
    st.dataframe(rows, width="stretch")

    st.divider()
    st.subheader("Create user")
    with st.form("new_user"):
        email = st.text_input("Email")
        name = st.text_input("Name")
        password = st.text_input("Initial password", type="password")
        role = st.selectbox("Role", ["user", "admin"])
        submit = st.form_submit_button("Create")
    if submit:
        if not email or not name or not password:
            st.error("Fill all fields.")
        else:
            try:
                u = create_user(email, name, password, role)
                st.success(f"Created user #{u.id} {u.email}")
                st.rerun()
            except Exception as e:
                st.error(f"Failed: {e}")

    st.divider()
    st.subheader("Reset password / deactivate")
    with session() as s:
        users = s.execute(select(User).order_by(User.id)).scalars().all()
        opts = {f"#{u.id} · {u.email} ({u.role})": u.id for u in users}
    pick = st.selectbox("User", list(opts.keys()) if opts else ["(no users)"])
    if pick and pick != "(no users)":
        target_id = opts[pick]
        c1, c2 = st.columns(2)
        with c1:
            new_pw = st.text_input("New password", key="adm_pw", type="password")
            if st.button("Reset password"):
                if new_pw and len(new_pw) >= 4:
                    update_password(target_id, new_pw)
                    st.success("Password reset.")
                else:
                    st.error("Min 4 chars.")
        with c2:
            with session() as s:
                u = s.get(User, target_id)
                cur_active = u.is_active
            if st.button(("Deactivate" if cur_active else "Activate")):
                with session() as s:
                    u = s.get(User, target_id)
                    u.is_active = not u.is_active
                    s.add(u)
                st.rerun()


# ============ Projects list / create / detail (single page) ============
def view_projects():
    require_login()
    u = current_user()

    # If a project is selected, render detail inline (with back button)
    if st.session_state.get("current_project_id"):
        if st.button("← Back to projects list"):
            st.session_state.pop("current_project_id", None)
            st.rerun()
        view_project_detail()
        return

    # ----- list view -----
    st.title("Projects")
    projects = visible_projects(u["id"])
    if not projects:
        st.info("No projects yet. Create one below.")
    else:
        st.caption(f"{len(projects)} project(s) · click Open to view")
        for p in projects:
            with session() as s:
                p2 = s.get(Project, p.id)
                n_caps = len(p2.captures); n_docs = len(p2.documents); n_mats = len(p2.materials)
            with st.container(border=True):
                c1, c2, c3 = st.columns([3, 2, 1])
                c1.markdown(f"### {p.name}")
                if p.address:
                    c1.caption(p.address)
                c2.markdown(f"**status:** `{p.status}`")
                c2.caption(f"captures: {n_caps} · docs: {n_docs} · materials: {n_mats}")
                if c3.button("Open", key=f"open_{p.id}", type="primary"):
                    st.session_state["current_project_id"] = p.id
                    st.rerun()

    st.divider()
    st.subheader("Create new project")
    with st.form("new_project", clear_on_submit=True):
        name = st.text_input("Name")
        address = st.text_input("Address (optional)")
        description = st.text_area("Description (optional)")
        status = st.selectbox("Status", ["planning", "in_progress", "completed", "archived"])
        submit = st.form_submit_button("Create", type="primary")
    if submit:
        if not name:
            st.error("Name required.")
        else:
            with session() as s:
                p = Project(name=name, address=address or None,
                            description=description or None, status=status,
                            owner_id=u["id"])
                s.add(p); s.flush()
                pid = p.id
            project_dir(pid)
            st.success(f"Created project #{pid} '{name}'")
            st.session_state["current_project_id"] = pid
            st.rerun()


# ============ Project detail (tabs) ============
def view_project_detail():
    require_login()
    pid = st.session_state.get("current_project_id")
    if not pid:
        st.warning("Pick a project from the Projects page.")
        return
    with session() as s:
        p = s.get(Project, pid)
        if not p:
            st.error("Project not found.")
            return
        owner = s.get(User, p.owner_id)
        s.expunge(p); s.expunge(owner)

    st.title(f"📂 {p.name}")
    st.caption(f"Owner: {owner.name} · status: {p.status}"
               + (f" · {p.address}" if p.address else ""))

    tabs = st.tabs(["Overview", "Documents", "Materials", "Captures", "Analyses", "Settings"])
    with tabs[0]: _proj_overview(p)
    with tabs[1]: _proj_documents(p)
    with tabs[2]: _proj_materials(p)
    with tabs[3]: _proj_captures(p)
    with tabs[4]: _proj_analyses(p)
    with tabs[5]: _proj_settings(p)


def _proj_overview(p: Project):
    with session() as s:
        p2 = s.get(Project, p.id)
        n_docs = len(p2.documents); n_mats = len(p2.materials)
        n_caps = len(p2.captures); n_analyses = len(p2.analyses)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Documents", n_docs)
    c2.metric("Materials", n_mats)
    c3.metric("Captures", n_caps)
    c4.metric("Analyses", n_analyses)
    if p.description:
        st.markdown(p.description)


def _run_3d_modeling(doc_id: int, project_id: int):
    """为施工照片运行3D建模（使用Aholo 3DGS API）."""
    import subprocess
    import sys
    import uuid

    with session() as s:
        d = s.get(Document, doc_id)
        if not d:
            st.error("文档未找到。")
            return
        doc_path = project_dir(project_id) / d.path

    if not doc_path.exists():
        st.error(f"文件不存在: {doc_path}")
        return

    # 检查文件类型（只支持图片）
    suffix = doc_path.suffix.lower()
    if suffix not in ['.jpg', '.jpeg', '.png']:
        st.error("Aholo 3D建模只支持图片(JPG/PNG)文件。")
        return

    # 创建专用的3D建模任务
    job_id = f"job_3d_{beijing_timestamp()}_{uuid.uuid4().hex[:4]}_doc{doc_id}"
    jdir = jobs_dir(project_id)
    jdir.mkdir(parents=True, exist_ok=True)

    st.info(f"启动Aholo 3D建模任务 **{job_id}**...")

    # 准备输出目录
    outputs_dir = project_dir(project_id) / "3d_models" / f"doc_{doc_id}"
    outputs_dir.mkdir(parents=True, exist_ok=True)

    # 创建状态文件
    status_path = jdir / f"{job_id}.json"
    status_data = {
        "job_id": job_id,
        "doc_id": doc_id,
        "project_id": project_id,
        "overall_status": "running",
        "provider": "aholo3d",
        "started_at": beijing_timestamp("%Y-%m-%d %H:%M:%S"),
        "stages": [
            {"name": "upload", "status": "pending"},
            {"name": "create_task", "status": "pending"},
            {"name": "processing", "status": "pending"},
            {"name": "download", "status": "pending"},
        ]
    }
    status_path.write_text(json.dumps(status_data, indent=2))

    # 启动后台进程调用Aholo 3D API
    cmd = [
        sys.executable, str(Path(__file__).resolve().parent.parent / "scripts" / "run_aholo3d.py"),
        "--job-id", job_id,
        "--doc-id", str(doc_id),
        "--project-id", str(project_id),
        "--image-path", str(doc_path),
        "--output-dir", str(outputs_dir),
        "--status-path", str(status_path),
    ]

    # 启动后台进程
    log = (jdir / f"{job_id}.log").open("w")
    subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

    st.success(f"3D建模任务 **{job_id}** 已启动。任务完成后可在下方查看结果。")
    st.session_state["last_3d_job_id"] = job_id
    st.rerun()


def _proj_documents(p: Project):
    st.subheader("Upload document")
    with st.form("doc_upload", clear_on_submit=True):
        cat_label_to_key = {label: key for key, label in DOCUMENT_CATEGORIES}
        category_label = st.selectbox(
            "Category",
            [label for _, label in DOCUMENT_CATEGORIES],
            help="Choose the document category from the project's data manifest.",
        )
        files = st.file_uploader(
            "Files (PDF, Word, Excel, IFC, PPT, JPG, MP4, DWG, RVT, …)",
            accept_multiple_files=True,
        )
        submit = st.form_submit_button("Upload + analyze", type="primary")
    if submit:
        if not files:
            st.warning("No files selected.")
        else:
            cat_key = cat_label_to_key[category_label]
            target_dir = documents_dir(p.id, cat_key)
            n_ok = 0
            for f in files:
                dest = target_dir / f.name
                dest.write_bytes(f.getbuffer())
                doc_id = _save_document_record(
                    project_id=p.id, category=cat_key,
                    filename=f.name, path=str(dest.relative_to(project_dir(p.id))),
                    size_bytes=dest.stat().st_size,
                    uploaded_by_id=current_user()["id"],
                )
                _run_doc_analysis(doc_id, dest)
                n_ok += 1
            st.success(f"Uploaded + analyzed {n_ok} file(s).")
            st.rerun()

    st.divider()
    st.subheader("Documents in this project")
    with session() as s:
        p2 = s.get(Project, p.id)
        docs = sorted(p2.documents, key=lambda d: -d.id)
        rows_by_cat: dict[str, list] = {}
        for d in docs:
            rows_by_cat.setdefault(d.category, []).append({
                "id": d.id, "filename": d.filename,
                "size_kb": round(d.size_bytes / 1024, 1),
                "status": d.analysis_status,
                "summary": (d.analysis_summary or "")[:160],
                "uploaded_at": format_beijing(d.uploaded_at),
            })
    for key, label in DOCUMENT_CATEGORIES:
        rows = rows_by_cat.get(key, [])
        if rows:
            with st.expander(f"**{label}** — {len(rows)} file(s)", expanded=True):
                st.dataframe(rows, width="stretch", hide_index=True)
                doc_ids = [r["id"] for r in rows]
                pick = st.selectbox("Inspect document", doc_ids,
                                    format_func=lambda i: next(r["filename"] for r in rows if r["id"] == i),
                                    key=f"pick_{key}")
                c1, c2 = st.columns(2)
                with c1:
                    if pick and st.button("View analysis", key=f"view_{key}"):
                        _show_doc_detail(pick)
                with c2:
                    # 施工照片类别添加3D建模按钮
                    if key == "07_photos" and pick and st.button("3D建模", key=f"3d_{key}", type="primary"):
                        _run_3d_modeling(pick, p.id)

    # 3D建模任务状态查看
    st.divider()
    st.subheader("3D建模任务状态")
    jdir = jobs_dir(p.id)
    if jdir.exists():
        # 查找所有3D建模相关的job文件
        job_files = sorted(jdir.glob("job_3d_*.json"), reverse=True)[:10]
        if job_files:
            for jf in job_files:
                try:
                    d = json.loads(jf.read_text())
                    job_id = d.get("job_id", "unknown")
                    status = d.get("overall_status", "unknown")
                    # 获取对应文档信息
                    doc_id = None
                    if "_doc" in job_id:
                        try:
                            doc_id = int(job_id.split("_doc")[-1].split("_")[0])
                        except:
                            pass

                    status_emoji = {"pending": "⚪", "running": "🟡", "done": "✅", "failed": "❌"}.get(status, "•")

                    with st.expander(f"{status_emoji} **{job_id}** — {status}", expanded=(status == "running")):
                        st.caption(f"开始时间: {d.get('started_at', '—')}")
                        st.caption(f"更新时间: {d.get('updated_at', '—')}")
                        if doc_id:
                            st.caption(f"关联文档ID: #{doc_id}")

                        # 显示Aholo3d特有信息
                        provider = d.get("provider", "colmap")
                        if provider == "aholo3d":
                            st.caption(f"🔧 服务商: Aholo 3DGS")
                            if "world_id" in d:
                                st.caption(f"🌍 World ID: `{d['world_id']}`")
                            if "asset_id" in d:
                                st.caption(f"📦 Asset ID: `{d['asset_id']}`")
                            if "progress" in d:
                                st.progress(d["progress"] / 100, text=f"处理进度: {d['progress']:.1f}%")

                        # 显示各阶段状态
                        for s in d.get("stages", []):
                            emoji = {"pending": "⚪", "running": "🟡", "done": "✅", "failed": "❌", "skipped": "⏭️"}.get(s["status"], "•")
                            msg = f" — {s.get('message', '')}" if s.get("message") else ""
                            # 中文阶段名映射
                            stage_names = {
                                "upload": "上传图片",
                                "create_task": "创建任务",
                                "processing": "3D重建",
                                "download": "下载结果",
                                "extract_frames": "提取帧",
                                "reconstruct": "点云重建",
                            }
                            stage_name = stage_names.get(s["name"], s["name"])
                            st.markdown(f"{emoji} **{stage_name}** · {s['status']}{msg}")

                        # 显示日志
                        log_p = jdir / f"{job_id}.log"
                        if log_p.exists():
                            with st.expander("查看日志"):
                                log_content = log_p.read_text()
                                st.code(log_content[-4000:] if len(log_content) > 4000 else log_content, language="text")

                        # 检查生成的文件
                        if doc_id:
                            output_dir = project_dir(p.id) / "3d_models" / f"doc_{doc_id}"
                            # 查找所有3D模型文件
                            model_files = list(output_dir.rglob("*.ply")) + list(output_dir.rglob("*.splat")) + list(output_dir.rglob("*.obj"))

                            if model_files:
                                st.success(f"✅ 已生成 {len(model_files)} 个3D模型文件:")
                                for f in model_files:
                                    col1, col2 = st.columns([3, 1])
                                    col1.code(str(f.relative_to(project_dir(p.id))))
                                    # 提供下载按钮
                                    with open(f, "rb") as file:
                                        col2.download_button(
                                            "下载",
                                            data=file.read(),
                                            file_name=f.name,
                                            mime="application/octet-stream",
                                            key=f"dl_{job_id}_{f.name}",
                                        )

                                # 在Point Cloud页面查看按钮
                                ply_files = [f for f in model_files if f.suffix == ".ply"]
                                if ply_files and st.button("在Point Cloud页面查看", key=f"view_pcd_{job_id}"):
                                    st.session_state["goto_pointcloud"] = str(ply_files[0])
                                    st.rerun()
                            elif status == "done":
                                st.warning("⚠️ 任务已完成，但未找到3D模型文件")
                except Exception as e:
                    st.error(f"读取任务文件失败: {e}")
        else:
            st.info("暂无3D建模任务")
    else:
        st.info("暂无3D建模任务")


def _save_document_record(*, project_id, category, filename, path, size_bytes, uploaded_by_id) -> int:
    with session() as s:
        d = Document(
            project_id=project_id, category=category, filename=filename,
            path=path, size_bytes=size_bytes, uploaded_by_id=uploaded_by_id,
        )
        s.add(d); s.flush()
        return d.id


def _run_doc_analysis(doc_id: int, abs_path: Path):
    with session() as s:
        d = s.get(Document, doc_id)
        d.analysis_status = "running"
        s.add(d)
    res = run_analyzer(abs_path)
    with session() as s:
        d = s.get(Document, doc_id)
        d.analysis_status = res["status"]
        d.analysis_summary = res.get("summary", "")[:1024]
        d.analysis_data_json = serialize(res.get("data") or {})
        d.analysis_error = res.get("error")
        d.analyzed_at = now_utc()
        s.add(d)


def _show_doc_detail(doc_id: int):
    with session() as s:
        d = s.get(Document, doc_id)
        if not d:
            st.error("Not found.")
            return
        category = d.category
        filename = d.filename
        status = d.analysis_status
        summary = d.analysis_summary
        data_raw = d.analysis_data_json
        error = d.analysis_error
        path = d.path
    st.markdown(f"### {filename}")
    st.caption(f"category: `{category}` · status: **{status}**")
    if summary:
        st.markdown(f"**Summary:** {summary}")
    if error:
        with st.expander("Error trace"):
            st.code(error)
    if data_raw:
        try:
            data = json.loads(data_raw)
        except Exception:
            data = {}
        if data.get("text_preview"):
            with st.expander("Text preview"):
                st.text(data["text_preview"])
        if data.get("sheets"):
            for sh in data["sheets"][:5]:
                with st.expander(f"Sheet · {sh['name']} ({sh.get('n_rows','?')} rows × {sh.get('n_cols','?')} cols)"):
                    if sh.get("preview"):
                        st.dataframe(sh["preview"], width="stretch")
        if data.get("element_counts"):
            st.markdown("**IFC element counts:**")
            st.json(data["element_counts"])
        if data.get("slides"):
            for sl in data["slides"][:10]:
                st.markdown(f"**Slide {sl['index']+1}** — {sl['title']}")
                st.text(sl["body"][:500])
        if data.get("exif"):
            with st.expander("EXIF"):
                st.json(data["exif"])
    # Download button
    abs_path = project_dir(d.project_id) / path if "d" in dir() and hasattr(d, "project_id") else None
    # Re-fetch inside session for download
    with session() as s:
        d = s.get(Document, doc_id)
        abs_path = project_dir(d.project_id) / d.path
    if abs_path.exists():
        st.download_button(
            "Download original",
            data=abs_path.read_bytes(),
            file_name=filename,
        )


def _proj_materials(p: Project):
    st.subheader("Material library (per-project SKUs)")
    with st.form("new_material"):
        c1, c2, c3 = st.columns([2, 1, 1])
        with c1: name = st.text_input("SKU name")
        with c2: mtype = st.selectbox("Type", MATERIAL_TYPES)
        with c3: vendor = st.text_input("Vendor")
        c1, c2, c3 = st.columns(3)
        with c1: color = st.text_input("Color")
        with c2: dim_w = st.number_input("Width (mm)", 0.0, 100000.0, 0.0)
        with c3: dim_h = st.number_input("Height (mm)", 0.0, 100000.0, 0.0)
        c1, c2 = st.columns(2)
        with c1: unit = st.text_input("Unit (m2 / piece / m / kg)")
        with c2: price = st.number_input("Price (per unit)", 0.0, 1e9, 0.0)
        notes = st.text_area("Notes")
        images = st.file_uploader("Reference images", accept_multiple_files=True,
                                   type=["jpg", "jpeg", "png"])
        submit = st.form_submit_button("Add material")
    if submit:
        if not name:
            st.error("SKU name required.")
        else:
            slug = slugify(name)
            # Ensure slug uniqueness within project
            with session() as s:
                existing = s.execute(
                    select(Material).where(Material.project_id == p.id, Material.sku_name == name)
                ).scalar_one_or_none()
                if existing:
                    st.error(f"Material '{name}' already exists in this project.")
                    return
                m = Material(project_id=p.id, sku_name=name, type=mtype,
                             vendor=vendor or None, color=color or None,
                             dim_w_mm=dim_w or None, dim_h_mm=dim_h or None,
                             unit=unit or None, price=price or None,
                             notes=notes or None,
                             images_dir=f"materials/{slug}/images")
                s.add(m); s.flush()
                mid = m.id
            mdir = material_slug_dir(p.id, slug)
            for f in images or []:
                (mdir / "images" / f.name).write_bytes(f.getbuffer())
            st.success(f"Added material #{mid} '{name}' (slug={slug}) with {len(images or [])} image(s).")
            st.rerun()

    st.divider()
    with session() as s:
        p2 = s.get(Project, p.id)
        mats = sorted(p2.materials, key=lambda m: -m.id)
        rows = [{"id": m.id, "name": m.sku_name, "type": m.type, "vendor": m.vendor,
                 "color": m.color, "unit": m.unit, "price": m.price,
                 "size_mm": f"{m.dim_w_mm or '?'} × {m.dim_h_mm or '?'}"}
                for m in mats]
    if rows:
        st.dataframe(rows, width="stretch", hide_index=True)
    else:
        st.info("No materials yet.")


def _proj_captures(p: Project):
    import subprocess, sys, time, uuid
    st.subheader("Captures")
    st.caption("A capture = one walkthrough video or photo set. Pipeline runs per capture.")

    with session() as s:
        p2 = s.get(Project, p.id)
        caps = sorted(p2.captures, key=lambda c: -c.id)

    # Existing captures + per-capture pipeline button
    if caps:
        for c in caps:
            with st.container(border=True):
                col1, col2, col3, col4 = st.columns([2, 1, 1, 1])
                col1.markdown(f"**{c.name}** · `id={c.id}`")
                col1.caption(f"captured: {format_beijing(c.captured_at, fallback='—')} · "
                             f"frames: {c.frames_count or 0}")
                col2.markdown(f"**status:** {c.status}")
                # Find latest job for this capture
                with session() as s:
                    latest_job_path = None
                    jdir = jobs_dir(p.id)
                    for jf in sorted(jdir.glob(f"job_*_cap{c.id}.json"), reverse=True):
                        latest_job_path = jf
                        break
                if col3.button("Run pipeline", key=f"run_{c.id}",
                               disabled=(c.status == "processing")):
                    job_id = f"job_{beijing_timestamp()}_{uuid.uuid4().hex[:4]}_cap{c.id}"
                    cmd = [
                        sys.executable, str(Path(__file__).resolve().parent.parent / "scripts" / "run_pipeline.py"),
                        "--job-id", job_id,
                        "--project-id", str(p.id),
                        "--capture-id", str(c.id),
                    ]
                    log = (jdir / f"{job_id}.subprocess.log").open("w")
                    subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                    st.success(f"Started job **{job_id}**.")
                    st.rerun()
                if col4.button("Inspect", key=f"insp_{c.id}"):
                    st.session_state["inspect_capture_id"] = c.id

                if latest_job_path and latest_job_path.exists():
                    try:
                        d = json.loads(latest_job_path.read_text())
                        with st.expander(f"latest job · {d.get('overall_status','?')}",
                                          expanded=(d.get("overall_status") == "running")):
                            for stg in d.get("stages", []):
                                emoji = {"pending":"⚪","running":"🟡","done":"✅",
                                         "failed":"❌","skipped":"⏭️"}.get(stg["status"], "•")
                                msg = f" — {stg.get('message','')}" if stg.get('message') else ""
                                st.markdown(f"{emoji} **{stg['name']}** · {stg['status']}{msg}")
                            log_p = jdir / f"{d['job_id']}.log"
                            if log_p.exists():
                                with st.expander("Full log"):
                                    st.code(log_p.read_text()[-6000:], language="text")
                    except Exception:
                        pass

                # Inspect panel
                if st.session_state.get("inspect_capture_id") == c.id:
                    _capture_inspect(p.id, c)
    else:
        st.info("No captures yet. Upload a video below to start.")

    st.divider()
    st.subheader("Add new capture (upload video)")
    with st.form("new_capture", clear_on_submit=True):
        c1, c2 = st.columns([2, 1])
        with c1: cap_name = st.text_input("Capture name", value=f"Capture {now_beijing().strftime('%Y-%m-%d %H:%M')}")
        with c2: notes = st.text_input("Notes (optional)")
        video = st.file_uploader("Video (mp4 / mov)", type=["mp4", "mov", "MOV", "MP4", "m4v", "mkv"])
        run_now = st.checkbox("Run pipeline immediately after upload", value=True)
        submit = st.form_submit_button("Create capture")
    if submit and video:
        with session() as s:
            c = Capture(project_id=p.id, name=cap_name, notes=notes or None,
                        captured_by_id=current_user()["id"], status="uploaded")
            s.add(c); s.flush()
            cid = c.id
        cdir = capture_dir(p.id, cid)
        dst = cdir / f"raw{Path(video.name).suffix.lower()}"
        dst.write_bytes(video.getbuffer())
        rel = str(dst.relative_to(project_dir(p.id)))
        with session() as s:
            c = s.get(Capture, cid)
            c.src_video_path = rel
            c.frames_dir = f"captures/{cid}/frames"
            c.outputs_dir = f"captures/{cid}/outputs"
            s.add(c)
        if run_now:
            import subprocess, sys, uuid
            job_id = f"job_{beijing_timestamp()}_{uuid.uuid4().hex[:4]}_cap{cid}"
            cmd = [
                sys.executable, str(Path(__file__).resolve().parent.parent / "scripts" / "run_pipeline.py"),
                "--job-id", job_id,
                "--project-id", str(p.id),
                "--capture-id", str(cid),
            ]
            log = (jobs_dir(p.id) / f"{job_id}.subprocess.log").open("w")
            subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            st.success(f"Created capture #{cid} and started pipeline {job_id}.")
        else:
            st.success(f"Created capture #{cid}. Use the 'Run pipeline' button above when ready.")
        st.rerun()


def _capture_inspect(project_id: int, c: Capture):
    """Quick inspector — shows segmentation manifest counts + matches summary."""
    proot = project_dir(project_id)
    outputs = (proot / c.outputs_dir).resolve() if c.outputs_dir else None
    if not outputs or not outputs.exists():
        st.info("No outputs yet — run the pipeline.")
        return
    st.markdown(f"**Outputs:** `{outputs}`")
    manifest = outputs / "segments" / "manifest.json"
    summary = outputs / "summary.json"
    matches = outputs / "matches.json"
    if summary.exists():
        st.json(json.loads(summary.read_text()))
    if manifest.exists():
        m = json.loads(manifest.read_text())
        from collections import Counter
        cls_hist = Counter(x["class"].strip() for x in m).most_common(10)
        st.markdown(f"**Segmentation manifest:** {len(m)} instances · top classes: {cls_hist}")
    if matches.exists():
        mm = json.loads(matches.read_text())
        from collections import Counter
        sku_hist = Counter(v.get("sku") or "(none)" for v in mm.values()).most_common()
        st.markdown(f"**SKU match histogram:** {dict(sku_hist)}")


def _proj_analyses(p: Project):
    st.subheader("Analyses")
    st.info("Once two captures exist, generate a change report. (Wired next session.)")


def _proj_settings(p: Project):
    u = current_user()
    if u["id"] != p.owner_id and not is_admin():
        st.info("Only the owner or an admin can change settings.")
        return
    st.subheader("Edit project")
    with st.form("edit_project"):
        new_name = st.text_input("Name", value=p.name)
        new_addr = st.text_input("Address", value=p.address or "")
        new_desc = st.text_area("Description", value=p.description or "")
        new_status = st.selectbox("Status", ["planning", "in_progress", "completed", "archived"],
                                  index=["planning","in_progress","completed","archived"].index(p.status))
        save = st.form_submit_button("Save")
    if save:
        with session() as s:
            x = s.get(Project, p.id)
            x.name, x.address, x.description, x.status = new_name, new_addr or None, new_desc or None, new_status
            s.add(x)
        st.success("Saved.")
        st.rerun()
    st.divider()
    st.subheader("Members")
    ROLE_CHOICES = ["viewer", "editor"]
    with session() as s:
        ms = s.execute(select(ProjectMember).where(ProjectMember.project_id == p.id)).scalars().all()
        member_rows = []
        for m in ms:
            uu = s.get(User, m.user_id)
            member_rows.append({"user_id": m.user_id, "email": uu.email, "role": m.role,
                                "added": format_beijing(m.added_at, "%Y-%m-%d") if m.added_at else ""})
    if member_rows:
        with st.container(border=True):
            # Header row
            h1, h2, h3, h4 = st.columns([3, 2, 1, 1])
            h1.caption("User"); h2.caption("Role"); h3.caption("Added"); h4.caption("")
            for row in member_rows:
                st.divider()
                c1, c2, c3, c4 = st.columns([3, 2, 1, 1])
                c1.markdown(f"**{row['email']}**")
                cur_role = row["role"] if row["role"] in ROLE_CHOICES else ROLE_CHOICES[0]
                new_role = c2.selectbox(
                    "role", ROLE_CHOICES, index=ROLE_CHOICES.index(cur_role),
                    key=f"mrole_{row['user_id']}", label_visibility="collapsed",
                )
                c3.caption(row["added"])
                if new_role != row["role"]:
                    if c4.button("Save", key=f"msave_{row['user_id']}", type="primary"):
                        with session() as s:
                            m = s.get(ProjectMember, (p.id, row["user_id"]))
                            if m:
                                m.role = new_role
                                s.add(m)
                        st.success(f"Updated {row['email']} → {new_role}.")
                        st.rerun()
                else:
                    if c4.button("Remove", key=f"mdel_{row['user_id']}"):
                        with session() as s:
                            m = s.get(ProjectMember, (p.id, row["user_id"]))
                            if m:
                                s.delete(m)
                        st.success(f"Removed {row['email']}.")
                        st.rerun()
    else:
        st.caption("No members yet.")
    with session() as s:
        all_users = s.execute(select(User).where(User.id != p.owner_id)).scalars().all()
        opts = {f"{u.email} ({u.role})": u.id for u in all_users}
    add_email = st.selectbox("Add member", list(opts.keys()) if opts else ["(no users)"])
    add_role = st.selectbox("Role", ["viewer", "editor"])
    if st.button("Add"):
        if add_email and add_email != "(no users)":
            uid = opts[add_email]
            added = False
            with session() as s:
                exist = s.execute(select(ProjectMember).where(
                    ProjectMember.project_id == p.id,
                    ProjectMember.user_id == uid,
                )).scalar_one_or_none()
                if exist is None:
                    s.add(ProjectMember(project_id=p.id, user_id=uid, role=add_role))
                    added = True
            # NB: st.rerun() raises a BaseException, so it must run *outside*
            # the session() block — otherwise the commit is skipped and the
            # new member is silently dropped.
            if added:
                st.success("Member added.")
                st.rerun()
            else:
                st.warning("Already a member.")
        else:
            st.warning("No user selected. Create another user account first (Admin → Users).")
