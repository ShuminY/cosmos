"""任务追踪视图模块.

功能：
1. 任务创建 - 上传初始状态照片
2. 任务查询 - 列表展示所有任务
3. 任务编辑 - 更新信息、上传后续照片
4. 进度分析 - 对比图片生成进度报告
"""
from __future__ import annotations
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import streamlit as st
from PIL import Image

import json

from src.db import session, Task, TaskSnapshot, TaskAnalysis, Project, User
from src.storage import project_dir


def view_task_tracking(project: Project):
    """任务追踪主页面."""
    st.title("📋 任务追踪")
    st.caption("施工任务进度管理与追踪")

    # 如果没有选中项目，提示用户选择
    if project is None:
        st.warning("请先在 Projects 页面选择一个项目")
        if st.button("前往 Projects 页面"):
            st.rerun()
        return

    # 子页面导航
    tab_list, tab_create, tab_analysis = st.tabs([
        "📋 任务列表", "➕ 新建任务", "📊 进度分析"
    ])

    with tab_list:
        _task_list(project)

    with tab_create:
        _task_create(project)

    with tab_analysis:
        _task_analysis(project)


def _task_list(project: Project):
    """任务列表与查询."""
    st.subheader("任务列表")

    # 筛选条件
    col1, col2, col3 = st.columns(3)
    with col1:
        status_filter = st.multiselect(
            "状态筛选",
            ["pending", "in_progress", "completed", "cancelled"],
            default=[],
            format_func=lambda x: {
                "pending": "⏳ 待开始",
                "in_progress": "🔄 进行中",
                "completed": "✅ 已完成",
                "cancelled": "❌ 已取消",
            }.get(x, x),
        )
    with col2:
        priority_filter = st.multiselect(
            "优先级筛选",
            ["low", "medium", "high", "urgent"],
            default=[],
            format_func=lambda x: {
                "low": "🟢 低",
                "medium": "🟡 中",
                "high": "🟠 高",
                "urgent": "🔴 紧急",
            }.get(x, x),
        )
    with col3:
        search = st.text_input("🔍 搜索任务", placeholder="输入任务名称...")

    # 查询任务（使用joinedload预加载snapshots避免DetachedInstanceError）
    with session() as s:
        from sqlalchemy.orm import joinedload
        query = s.query(Task).options(joinedload(Task.snapshots), joinedload(Task.analyses)).filter(Task.project_id == project.id)

        if status_filter:
            query = query.filter(Task.status.in_(status_filter))
        if priority_filter:
            query = query.filter(Task.priority.in_(priority_filter))
        if search:
            query = query.filter(Task.name.contains(search))

        tasks = query.order_by(Task.created_at.desc()).all()

    if not tasks:
        st.info("暂无任务，请点击「新建任务」创建")
        return

    # 显示统计
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("总任务", len(tasks))
    col2.metric("进行中", sum(1 for t in tasks if t.status == "in_progress"))
    col3.metric("已完成", sum(1 for t in tasks if t.status == "completed"))
    col4.metric("待开始", sum(1 for t in tasks if t.status == "pending"))

    st.divider()

    # 任务列表
    for task in tasks:
        with st.expander(_format_task_title(task), expanded=False):
            _task_detail(task, project)


def _format_task_title(task: Task) -> str:
    """格式化任务标题."""
    status_emoji = {
        "pending": "⏳",
        "in_progress": "🔄",
        "completed": "✅",
        "cancelled": "❌",
    }.get(task.status, "⏳")

    priority_emoji = {
        "low": "🟢",
        "medium": "🟡",
        "high": "🟠",
        "urgent": "🔴",
    }.get(task.priority, "🟡")

    return f"{status_emoji} {priority_emoji} {task.name}"


def _task_detail(task: Task, project: Project):
    """显示任务详情和编辑功能."""
    # 检查3D建模状态（如果有正在进行的任务）
    if task.model_3d_info:
        model_info = json.loads(task.model_3d_info)
        if model_info.get("status") == "running":
            job_id = model_info.get("job_id", "")
            from src.storage import jobs_dir
            status_path = jobs_dir(project.id) / f"{job_id}.json"
            if status_path.exists():
                status_data = json.loads(status_path.read_text())
                if status_data.get("overall_status") in ("done", "failed"):
                    # 更新状态
                    ply_files = list((project_dir(project.id) / model_info.get("output_dir", "")).rglob("*.ply"))
                    model_info["status"] = status_data["overall_status"]
                    model_info["ply_path"] = str(ply_files[0].relative_to(project_dir(project.id))) if ply_files else None
                    model_info["output_files"] = [str(f.relative_to(project_dir(project.id))) for f in ply_files]
                    model_info["viewer_url"] = status_data.get("viewer_url")
                    with session() as s:
                        t = s.get(Task, task.id)
                        t.model_3d_info = json.dumps(model_info)
                        s.add(t)
                        s.commit()
                    task.model_3d_info = json.dumps(model_info)

    col1, col2 = st.columns([2, 1])

    with col1:
        st.markdown(f"**描述:** {task.description or '无描述'}")
        st.markdown(f"**状态:** {_format_status(task.status)}")
        st.markdown(f"**优先级:** {_format_priority(task.priority)}")
        st.markdown(f"**创建时间:** {task.created_at.strftime('%Y-%m-%d %H:%M')}")
        if task.due_date:
            st.markdown(f"**截止日期:** {task.due_date.strftime('%Y-%m-%d')}")

        
    with col2:
        # 状态切换按钮
        if task.status == "pending":
            if st.button("▶️ 开始任务", key=f"start_{task.id}"):
                _update_task_status(task.id, "in_progress")
                st.success("任务已开始！")
                st.rerun()
        if task.status == "in_progress":
            if st.button("✅ 标记完成", key=f"complete_{task.id}"):
                _update_task_status(task.id, "completed")
                st.success("任务已标记为完成！")
                st.rerun()

        # AI进度分析按钮（pending和in_progress都显示）
        if task.status in ("pending", "in_progress"):
            if st.button("📊 AI进度分析", key=f"analysis_{task.id}"):
                _run_ai_progress_analysis(task, project)
                st.rerun()

        # 3D建模按钮（需要至少1张照片）
        if len(task.snapshots) >= 1:
            model_3d_info = json.loads(task.model_3d_info) if task.model_3d_info else None
            if model_3d_info and model_3d_info.get("status") == "done":
                viewer_url = model_3d_info.get("viewer_url")
                if viewer_url:
                    st.markdown(f'<a href="{viewer_url}" target="_blank">🌐 在Aholo Studio查看3D模型</a>',
                                unsafe_allow_html=True)
                # 如果有本地 PLY 文件，也可以点击查看
                output_files = model_3d_info.get("output_files") or []
                ply_path = model_3d_info.get("ply_path")
                output_path = ply_path or (output_files[0] if output_files else None)
                if output_path:
                    if st.button("📍 在Point Cloud页面查看", key=f"view3d_{task.id}"):
                        st.session_state["goto_pointcloud"] = str(project_dir(project.id) / output_path)
                        st.rerun()
            elif model_3d_info and model_3d_info.get("status") == "running":
                st.caption("🟡 3D建模进行中...")
                if st.button("↻ 刷新状态", key=f"refresh3d_{task.id}"):
                    st.rerun()
            else:
                if st.button("🏗️ 3D建模", key=f"model3d_{task.id}"):
                    _run_task_3d_modeling(task, project)
                    st.rerun()

        if st.button("🗑️ 删除", key=f"delete_{task.id}", type="secondary"):
            _delete_task(task.id)
            st.success("任务已删除！")
            st.rerun()

    # 照片快照
    st.divider()
    st.subheader("📷 现场照片")

    # 把最后一次的分析记录放在图片上方
    if task.analyses:
        latest_analysis = task.analyses[-1]
        snapshots_sorted = sorted(task.snapshots, key=lambda x: x.created_at)
        latest = snapshots_sorted[-1] if len(snapshots_sorted) > 1 else None
        base_title = Path(latest_analysis.base_image).stem if latest_analysis.base_image else "初始"
        compare_title = Path(latest_analysis.compare_image).stem if latest_analysis.compare_image else "最新"
        with st.container(border=True):
            st.caption(f"📊 **最近分析**（{latest_analysis.created_at.strftime('%m-%d %H:%M')}）基准: {base_title} | 对比: {compare_title}")
            st.markdown(latest_analysis.result)

    if task.snapshots:
        cols = st.columns(min(4, len(task.snapshots)))
        for i, snapshot in enumerate(task.snapshots):
            with cols[i % len(cols)]:
                snapshot_path = project_dir(project.id) / snapshot.file_path
                if snapshot_path.exists():
                    st.image(str(snapshot_path), caption=snapshot.title, width="stretch")
                    st.caption(f"📅 {snapshot.created_at.strftime('%Y-%m-%d %H:%M')} · 📁 {snapshot.title}")
                else:
                    st.error(f"文件不存在: {snapshot.title}")
    else:
        st.info("暂无照片")

    # 把历史记录放在图片下方
    if task.analyses:
        st.markdown("---")
        st.caption(f"📋 全部历史分析记录（{len(task.analyses)} 条）")
        for analysis in task.analyses[:-1]:  # 最新的已经在上面显示了
            label = f"📊 #{analysis.id} — {analysis.created_at.strftime('%m-%d %H:%M')}"
            if analysis.base_image:
                label += f"  ({Path(analysis.base_image).stem} → {Path(analysis.compare_image).stem})"
            with st.expander(label, expanded=False):
                st.markdown(analysis.result)

    # 添加快照按钮
    with st.expander("➕ 添加快照照片"):
        _add_snapshot_form(task, project)


def _add_snapshot_form(task: Task, project: Project):
    """添加快照表单."""
    with st.form(f"add_snapshot_{task.id}"):
        snapshot_type = st.selectbox(
            "照片类型",
            ["progress", "final"],
            format_func=lambda x: {
                "initial": "📸 初始状态",
                "progress": "🔄 进度更新",
                "final": "✅ 完成状态",
            }.get(x, x),
        )
        title = st.text_input("照片标题", value=f"进度更新 {datetime.now().strftime('%m-%d')}")
        description = st.text_area("描述", placeholder="描述当前进度...")
        uploaded = st.file_uploader("选择照片", type=["jpg", "jpeg", "png"])

        if st.form_submit_button("上传", type="primary"):
            if uploaded:
                _save_snapshot(task.id, project.id, snapshot_type, title, description, uploaded)
                # 上传快照后自动将任务状态改为 in_progress
                if task.status == "pending":
                    _update_task_status(task.id, "in_progress")
                st.success("照片已上传！")
                st.rerun()
            else:
                st.error("请选择照片")


def _task_create(project: Project):
    """创建新任务."""
    st.subheader("新建任务")

    with st.form("create_task"):
        name = st.text_input("任务名称 *", placeholder="输入任务名称...")
        description = st.text_area("任务描述", placeholder="描述任务内容、要求...")

        col1, col2 = st.columns(2)
        with col1:
            priority = st.selectbox(
                "优先级",
                ["low", "medium", "high", "urgent"],
                format_func=lambda x: {
                    "low": "🟢 低",
                    "medium": "🟡 中",
                    "high": "🟠 高",
                    "urgent": "🔴 紧急",
                }.get(x, x),
            )
        with col2:
            due_date = st.date_input(
                "截止日期",
                value=datetime.now() + timedelta(days=7),
                min_value=datetime.now(),
            )

        st.divider()
        st.subheader("📸 初始状态照片")
        st.caption("上传任务开始前的现场照片，用于后续进度对比")

        initial_title = st.text_input("照片标题", value="初始状态")
        initial_desc = st.text_area("照片描述", placeholder="描述初始状态...")
        initial_photo = st.file_uploader("选择初始照片", type=["jpg", "jpeg", "png"])

        submitted = st.form_submit_button("创建任务", type="primary")

    if submitted:
        if not name:
            st.error("请输入任务名称")
            return

        # 创建任务
        task_id = _create_task(
            project_id=project.id,
            name=name,
            description=description,
            priority=priority,
            due_date=due_date,
            created_by_id=st.session_state.get("user", {}).get("id"),
        )

        # 上传初始照片
        if initial_photo:
            _save_snapshot(
                task_id=task_id,
                project_id=project.id,
                snapshot_type="initial",
                title=initial_title or "初始状态",
                description=initial_desc,
                uploaded_file=initial_photo,
            )
            # 创建任务时上传照片自动转为进行中
            _update_task_status(task_id, "in_progress")

        st.success(f"任务「{name}」创建成功！")
        st.rerun()


def _task_analysis(project: Project):
    """任务进度分析."""
    st.subheader("📊 进度分析")
    st.caption("对比任务图片，分析施工进度")

    with session() as s:
        from sqlalchemy.orm import joinedload
        tasks = s.query(Task).options(joinedload(Task.snapshots)).filter(
            Task.project_id == project.id,
            Task.status.in_(["in_progress", "completed"])
        ).all()

    if not tasks:
        st.info("没有可分析的任务（需要状态为进行中或已完成）")
        return

    # 转为普通数据结构，避免 Streamlit deepcopy ORM 对象报错
    task_options = {t.id: t for t in tasks}

    # 选择任务
    selected_task_id = st.selectbox(
        "选择任务",
        list(task_options.keys()),
        format_func=lambda tid: f"{task_options[tid].name} ({len(task_options[tid].snapshots)} 张照片)",
    )

    if selected_task_id is None:
        return
    task = task_options[selected_task_id]

    if not task or len(task.snapshots) < 2:
        st.warning("该任务照片不足，无法进行进度对比（至少需要初始照片和一张进度照片）")
        return

    # 照片转普通数据结构
    snapshot_options = {s.id: s for s in sorted(task.snapshots, key=lambda x: x.created_at)}

    # 选择对比照片
    st.divider()
    col1, col2 = st.columns(2)

    with col1:
        st.subheader("📸 对比基准")
        base_id = st.selectbox(
            "选择基准照片（通常是初始状态）",
            list(snapshot_options.keys()),
            format_func=lambda sid: f"{_format_snapshot_type(snapshot_options[sid].snapshot_type)}: {snapshot_options[sid].title}",
            key="base_snapshot",
        )

    with col2:
        st.subheader("📸 当前进度")
        current_id = st.selectbox(
            "选择当前进度照片",
            list(snapshot_options.keys()),
            format_func=lambda sid: f"{_format_snapshot_type(snapshot_options[sid].snapshot_type)}: {snapshot_options[sid].title}",
            key="current_snapshot",
            index=len(snapshot_options) - 1,
        )

    if base_id and current_id:
        if st.button("🔍 生成进度分析报告", type="primary"):
            _generate_progress_report(snapshot_options[base_id], snapshot_options[current_id], project)


def _generate_progress_report(base: TaskSnapshot, current: TaskSnapshot, project: Project):
    """生成进度分析报告."""
    st.divider()
    st.subheader("📊 进度分析报告")

    base_path = project_dir(project.id) / base.file_path
    current_path = project_dir(project.id) / current.file_path

    if not base_path.exists() or not current_path.exists():
        st.error("照片文件不存在")
        return

    # 显示对比图
    col1, col2 = st.columns(2)
    with col1:
        st.markdown(f"**基准: {base.title}**")
        st.image(str(base_path), width="stretch")
        st.caption(f"{base.created_at.strftime('%Y-%m-%d %H:%M')}")

    with col2:
        st.markdown(f"**当前: {current.title}**")
        st.image(str(current_path), width="stretch")
        st.caption(f"{current.created_at.strftime('%Y-%m-%d %H:%M')}")

    # 简单的时间进度分析
    time_diff = current.created_at - base.created_at
    days_elapsed = time_diff.days

    st.divider()
    st.subheader("📈 进度统计")

    col1, col2, col3 = st.columns(3)
    col1.metric("时间跨度", f"{days_elapsed} 天")
    col2.metric("照片数量", len([s for s in base.task.snapshots]))

    # 状态变化
    if base.snapshot_type == "initial" and current.snapshot_type == "final":
        col3.metric("任务状态", "已完成", "✅")
    elif base.snapshot_type == "initial":
        col3.metric("任务状态", "进行中", "🔄")
    else:
        col3.metric("任务状态", "对比中", "📊")

    # 进度说明
    st.divider()
    st.subheader("📝 进度说明")

    progress_text = f"""
    **任务:** {base.task.name}

    **基准照片:** {base.title} ({_format_snapshot_type(base.snapshot_type)})
    - 拍摄时间: {base.created_at.strftime('%Y年%m月%d日 %H:%M')}
    - 描述: {base.description or '无'}

    **当前照片:** {current.title} ({_format_snapshot_type(current.snapshot_type)})
    - 拍摄时间: {current.created_at.strftime('%Y年%m月%d日 %H:%M')}
    - 描述: {current.description or '无'}

    **时间进度:**
    - 自基准照片以来已过去 {days_elapsed} 天
    - 照片间隔: {time_diff.total_seconds() / 3600:.1f} 小时

    **建议:**
    对比两张照片，观察施工区域的变化情况，评估是否符合预期进度。
    """
    st.markdown(progress_text)

    # 导出报告按钮
    if st.button("📥 导出PDF报告"):
        st.info("PDF导出功能开发中...")


# ============ Helper Functions ============

def _format_status(status: str) -> str:
    """格式化状态显示."""
    return {
        "pending": "⏳ 待开始",
        "in_progress": "🔄 进行中",
        "completed": "✅ 已完成",
        "cancelled": "❌ 已取消",
    }.get(status, status)


def _format_priority(priority: str) -> str:
    """格式化优先级显示."""
    return {
        "low": "🟢 低",
        "medium": "🟡 中",
        "high": "🟠 高",
        "urgent": "🔴 紧急",
    }.get(priority, priority)


def _format_snapshot_type(snapshot_type: str) -> str:
    """格式化快照类型显示."""
    return {
        "initial": "📸 初始状态",
        "progress": "🔄 进度更新",
        "final": "✅ 完成状态",
    }.get(snapshot_type, snapshot_type)


def _create_task(
    project_id: int,
    name: str,
    description: Optional[str],
    priority: str,
    due_date: Optional[datetime],
    created_by_id: Optional[int],
) -> int:
    """创建任务."""
    with session() as s:
        task = Task(
            project_id=project_id,
            name=name,
            description=description,
            priority=priority,
            due_date=due_date,
            created_by_id=created_by_id,
            status="pending",
        )
        s.add(task)
        s.flush()
        return task.id


def _update_task_status(task_id: int, status: str):
    """更新任务状态."""
    with session() as s:
        task = s.get(Task, task_id)
        if task:
            task.status = status
            if status == "completed":
                task.completed_at = datetime.utcnow()
            s.add(task)


def _delete_task(task_id: int):
    """删除任务."""
    with session() as s:
        task = s.get(Task, task_id)
        if task:
            s.delete(task)


def _save_snapshot(
    task_id: int,
    project_id: int,
    snapshot_type: str,
    title: str,
    description: Optional[str],
    uploaded_file,
):
    """保存快照照片."""
    # 保存文件
    proj_dir = project_dir(project_id)
    snapshots_dir = proj_dir / "task_snapshots" / f"task_{task_id}"
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    ext = Path(uploaded_file.name).suffix.lower()
    filename = f"{snapshot_type}_{timestamp}{ext}"
    file_path = snapshots_dir / filename

    with open(file_path, "wb") as f:
        f.write(uploaded_file.getbuffer())

    # 相对路径
    rel_path = file_path.relative_to(proj_dir)

    # 保存到数据库
    with session() as s:
        snapshot = TaskSnapshot(
            task_id=task_id,
            snapshot_type=snapshot_type,
            title=title,
            description=description,
            file_path=str(rel_path),
            file_size=len(uploaded_file.getbuffer()),
            created_by_id=st.session_state.get("user", {}).get("id"),
        )
        s.add(snapshot)


# ============ AI 进度分析 ============
def _run_ai_progress_analysis(task: Task, project: Project):
    """调用AI图片聊天进行进度分析，并保存结果."""
    # 获取初始照片和最新照片
    snapshots = sorted(task.snapshots, key=lambda x: x.created_at)
    initial = snapshots[0]
    latest = snapshots[-1]

    # 如果没有至少2张照片，跳过
    if len(snapshots) < 2:
        st.warning("需要至少2张照片（初始+进度）才能进行AI分析")
        return

    initial_path = project_dir(project.id) / initial.file_path
    latest_path = project_dir(project.id) / latest.file_path

    if not initial_path.exists() or not latest_path.exists():
        st.error("照片文件不存在，请重新上传")
        return

    # 构建提示词
    task_desc = task.description or "无任务描述"
    prompt = (
        "对比后一张图片和初始图片的差距，按照以下分类分别输出新增内容：\n"
        "- 正视墙（正对墙面）\n"
        "- 左视墙（左侧墙面）\n"
        "- 右视墙（右侧墙面）\n"
        "- 天花板\n"
        "- 地板\n\n"
        "新增内容包括但不限于：地砖、墙漆、门窗、灯具、插座等。\n\n"
        f"并且根据任务目标「{task_desc}」输出各项任务的百分比进度。\n\n"
        "请以清晰的分段方式输出结果。"
    )

    # 调用AI图片聊天
    from src.chatbot import simple_chat_image
    from src.settings import get_setting

    provider = get_setting("llm_default_provider", "openai")

    with st.spinner("🤖 AI正在分析进度..."):
        result = simple_chat_image(
            query=prompt,
            image_paths=[str(initial_path), str(latest_path)],
            project_id=project.id,
            user_id=st.session_state.get("user", {}).get("id"),
            provider=provider,
        )

    if result.get("error"):
        st.error(f"AI分析失败: {result['error']}")
        return

    answer = result.get("answer", "")

    # 保存为一条新的分析记录（历史进度）
    with session() as s:
        analysis = TaskAnalysis(
            task_id=task.id,
            result=answer,
            base_image=str(initial.file_path),
            compare_image=str(latest.file_path),
            images_used=json.dumps([str(initial.file_path), str(latest.file_path)]),
        )
        s.add(analysis)
        # 同时更新analysis_result保持兼容
        t = s.get(Task, task.id)
        t.analysis_result = answer
        s.add(t)
        s.commit()

    return answer


def _run_task_3d_modeling(task: Task, project: Project):
    """使用第一张照片进行3D建模."""
    import subprocess
    import sys
    import time
    import uuid

    # 获取第一张照片
    if not task.snapshots:
        st.warning("没有照片可用于3D建模")
        return

    first_snapshot = sorted(task.snapshots, key=lambda x: x.created_at)[0]
    image_path = project_dir(project.id) / first_snapshot.file_path

    if not image_path.exists():
        st.error(f"照片文件不存在: {image_path}")
        return

    # 创建输出目录
    model_dir = project_dir(project.id) / "3d_models" / f"task_{task.id}"
    model_dir.mkdir(parents=True, exist_ok=True)

    # 创建任务ID和状态文件
    job_id = f"job_3d_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}_task{task.id}"
    from src.storage import jobs_dir
    jdir = jobs_dir(project.id)
    jdir.mkdir(parents=True, exist_ok=True)

    status_path = jdir / f"{job_id}.json"
    status_data = {
        "job_id": job_id,
        "task_id": task.id,
        "project_id": project.id,
        "overall_status": "running",
        "provider": "aholo3d",
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "stages": [
            {"name": "upload", "status": "pending"},
            {"name": "create_task", "status": "pending"},
            {"name": "processing", "status": "pending"},
            {"name": "download", "status": "pending"},
        ]
    }
    status_path.write_text(json.dumps(status_data, indent=2))

    # 3D建模提示词
    prompt = "对住宅房间进行1:1三维模型还原，完全保留房间原有特征，还原现有布局与设施，不添加任何额外内容，地板需平行于x轴，墙面尽量为垂直于地板平面，且尽量保持平整，尽量减少像素点，不得删改房间内原有物品。"

    # 启动后台进程
    cmd = [
        sys.executable, str(Path(__file__).resolve().parent.parent / "scripts" / "run_aholo3d.py"),
        "--job-id", job_id,
        "--doc-id", str(task.id),
        "--project-id", str(project.id),
        "--image-path", str(image_path),
        "--output-dir", str(model_dir),
        "--status-path", str(status_path),
        "--prompt", prompt,
    ]

    log_path = jdir / f"{job_id}.log"
    log = log_path.open("w")
    subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

    # 保存3D建模信息到任务
    model_3d_info = {
        "job_id": job_id,
        "status": "running",
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "image": str(first_snapshot.file_path),
        "output_dir": str(model_dir.relative_to(project_dir(project.id))),
    }
    with session() as s:
        t = s.get(Task, task.id)
        t.model_3d_info = json.dumps(model_3d_info)
        s.add(t)
        s.commit()

    st.success(f"✅ 3D建模任务已启动 (job: {job_id})")
    st.info("建模完成后，点击「查看3D模型」按钮可在 Point Cloud 页面查看结果")


def _parse_analysis_sections(text: str) -> dict:
    """解析AI分析结果，按墙面/天花板/地板分段."""
    if not text:
        return {}

    sections = {}
    current_section = "📋 总体"
    current_content = []

    # 常见的分段标题关键词
    section_keywords = [
        "正视墙", "左视墙", "右视墙", "天花板", "地板",
        "总体进度", "百分比", "综合",
    ]

    for line in text.split("\n"):
        line_stripped = line.strip()
        # 检查是否为新段落标题
        is_new_section = False
        for kw in section_keywords:
            if kw in line_stripped and (
                line_stripped.startswith("#") or
                line_stripped.startswith("-") or
                line_stripped.startswith("*") or
                line_stripped.startswith(kw) or
                line_stripped.startswith("**")
            ):
                # 保存上一个段落
                if current_content:
                    sections[current_section] = "\n".join(current_content)

                current_section = f"📐 {kw}"
                current_content = [line_stripped]
                is_new_section = True
                break

        if not is_new_section:
            current_content.append(line_stripped)

    # 保存最后一个段落
    if current_content:
        sections[current_section] = "\n".join(current_content)

    return sections if sections else {"📋 分析结果": text}
