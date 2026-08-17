"""Construction-progress-monitoring POC — review UI.

Run from project root:
    source .venv/bin/activate
    streamlit run app/streamlit_app.py

Pages
- Overview     — what's set up, summary stats, ground truth
- Visits       — frames + segmentation overlay, side-by-side v1 vs v2
- Segments     — gallery of all detected instances
- Matches      — per-instance SKU similarities, accept/reject (HITL)
- References   — SKU library + ground truth
- Change report— final diff vs ground truth, confirm/dismiss

HITL feedback persists to outputs/hitl_feedback.json.
"""
from __future__ import annotations
import json
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import numpy as np
import streamlit as st
from PIL import Image
import plotly.graph_objects as go

# Optional 3D dependencies
try:
    import open3d as o3d
    OPEN3D_AVAILABLE = True
except ImportError:
    OPEN3D_AVAILABLE = False
    o3d = None

# Local imports for new multi-tenant views
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from views import (
    view_login, view_profile, view_admin_users,
    view_projects, view_project_detail, get_current_project,
    current_user, is_admin, require_login,
)
from views_kb import view_knowledge_base, view_chatbot, view_chat_image, view_llm_settings
from views_tasks import view_task_tracking
from views_drawings import view_drawing_analysis
from views_dwg2pdf import view_dwg2pdf
from views_manual_review import view_manual_review
from views_review_kb import view_review_knowledge_base
from src.db import init_db
from src.time_utils import beijing_timestamp
init_db()

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUTPUTS = ROOT / "outputs"
HITL_PATH = OUTPUTS / "hitl_feedback.json"

st.set_page_config(page_title="Cosmos — Construction Progress POC", layout="wide")


# ============ data loading ============
@st.cache_data(show_spinner=False)
def load_json(p: Path):
    return json.loads(Path(p).read_text()) if Path(p).exists() else None


def load_hitl() -> dict:
    if HITL_PATH.exists():
        return json.loads(HITL_PATH.read_text())
    return {"instance_decisions": {}, "change_decisions": {}}


def save_hitl(state: dict):
    HITL_PATH.parent.mkdir(parents=True, exist_ok=True)
    HITL_PATH.write_text(json.dumps(state, indent=2))


def list_frames(visit: int) -> list[Path]:
    return sorted((DATA / f"visit_{visit}" / "frames").glob("*.jpg"))


@st.cache_data(show_spinner=False)
def load_largest_pointcloud(visit: int):
    """Return (xyz, rgb) for the largest sparse PLY of this visit, or (None, None)."""
    if not OPEN3D_AVAILABLE:
        return None, None
    sparse = OUTPUTS / f"visit_{visit}" / "sparse"
    if not sparse.exists():
        return None, None
    candidates = list(sparse.glob("*/points3D.ply"))
    if not candidates:
        return None, None
    best = max(candidates, key=lambda p: len(o3d.io.read_point_cloud(str(p)).points))
    pcd = o3d.io.read_point_cloud(str(best))
    xyz = np.asarray(pcd.points)
    rgb = np.asarray(pcd.colors) if len(pcd.colors) else np.full((len(xyz), 3), 0.5)
    return xyz, rgb


@st.cache_data(show_spinner=False)
def load_gt_camera_centers(visit: int):
    """Ground-truth camera positions in world space (from synthetic data poses.json)."""
    p = DATA / f"visit_{visit}" / "poses.json"
    if not p.exists():
        return None
    poses = json.loads(p.read_text())["poses"]
    centers = []
    for rec in poses:
        Rt = np.array(rec["Rt_world_from_camera_inverse"])
        # Rt is world->camera. Camera center in world = -R^T @ t.
        R, t = Rt[:3, :3], Rt[:3, 3]
        c = -R.T @ t
        centers.append(c)
    return np.asarray(centers)


def overlay_masks(frame_path: Path, segments_dir: Path, alpha: float = 0.5) -> np.ndarray:
    """Composite all instance masks for one frame onto the image with random colors per instance."""
    img = cv2.cvtColor(cv2.imread(str(frame_path)), cv2.COLOR_BGR2RGB)
    frame_segs = segments_dir / frame_path.stem
    if not frame_segs.exists():
        return img
    overlay = img.copy()
    masks = sorted(frame_segs.glob("*_mask.png"))
    rng = np.random.default_rng(hash(frame_path.stem) % (2**32))
    for mp in masks:
        mask = cv2.imread(str(mp), cv2.IMREAD_GRAYSCALE)
        if mask is None:
            continue
        if mask.shape != img.shape[:2]:
            mask = cv2.resize(mask, (img.shape[1], img.shape[0]), interpolation=cv2.INTER_NEAREST)
        color = rng.integers(0, 256, 3).tolist()
        bool_mask = mask > 127
        overlay[bool_mask] = (np.array(color) * alpha + overlay[bool_mask] * (1 - alpha)).astype(np.uint8)
        # Outline
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(overlay, contours, -1, color, 2)
    return overlay


# ============ HITL state ============
if "hitl" not in st.session_state:
    st.session_state.hitl = load_hitl()


def set_decision(instance_id: str, decision: str, sku: str | None = None):
    st.session_state.hitl.setdefault("instance_decisions", {})[instance_id] = {
        "decision": decision, "sku": sku,
    }
    save_hitl(st.session_state.hitl)


# ============ auth gate ============
if not current_user():
    view_login()
    st.stop()

# ============ sidebar nav ============
u = current_user()
st.sidebar.title("Cosmos POC")
st.sidebar.markdown(f"**{u['name']}** · `{u['role']}`")
if st.sidebar.button("Log out"):
    st.session_state.pop("user", None)
    st.session_state.pop("current_project_id", None)
    st.rerun()
st.sidebar.divider()

nav_groups = {
    "📂 Projects": ["Projects"],
    "📋 任务追踪": ["Tasks"],
    "📝 人工审核": ["人工审核"],
    "📚 审图知识库": ["审图知识库"],
    "📐 图纸分析": ["图纸分析"],
    "📐 DWG转PDF": ["DWG转PDF"],
    "💡 智能问答": ["知识库", "智能问答", "图片问答"],
    "🧪 Demo data": ["Overview", "Upload", "Visits", "World model", "Point Cloud", "Segments",
                     "Matches (HITL)", "References", "Change report"],
    "👤 Account": ["Profile"],
}
if is_admin():
    nav_groups["🛠 Admin"] = ["Users", "LLM 设置"]

# Use one selectable row per page. Single-page groups use the group label itself
# to avoid duplicate-looking entries such as "📂 Projects" followed by "Projects".
nav_options = []
page_by_label = {}
for group_label, items in nav_groups.items():
    if len(items) == 1:
        nav_options.append(group_label)
        page_by_label[group_label] = items[0]
    else:
        for item in items:
            label = f"{group_label} · {item}"
            nav_options.append(label)
            page_by_label[label] = item

default_label = "📂 Projects"
choice = st.sidebar.radio(
    "Navigate",
    nav_options,
    index=nav_options.index(default_label) if default_label in nav_options else 0,
    label_visibility="collapsed",
)
page = page_by_label.get(choice, "Projects")

st.sidebar.divider()
v1_n = len(list_frames(1))
v2_n = len(list_frames(2))
st.sidebar.caption(f"demo: {v1_n} v1 frames · {v2_n} v2 frames")
n_decisions = len(st.session_state.hitl.get("instance_decisions", {}))
if n_decisions:
    st.sidebar.caption(f"HITL decisions: {n_decisions}")
    if st.sidebar.button("Reset HITL feedback", type="secondary"):
        st.session_state.hitl = {"instance_decisions": {}, "change_decisions": {}}
        save_hitl(st.session_state.hitl)
        st.rerun()


# ============ pages ============
def page_overview():
    st.title("Construction Progress POC")
    st.markdown(
        "Capture rooms before/after a renovation visit, "
        "auto-detect what changed (paint, tiles laid, doors, etc.), "
        "match against the user's SKU references."
    )

    report = load_json(OUTPUTS / "smoke_test_report.json")
    if not report:
        st.warning("No `outputs/smoke_test_report.json` yet — run the pipeline first.")
        return

    # Headline stats
    col1, col2, col3, col4 = st.columns(4)
    v1 = report["visit_1"]; v2 = report["visit_2"]; d = report["detected_changes"]
    col1.metric("Paint (v1 frames)", f"{v1['blue_paint']['frames']}/30")
    col2.metric("Paint (v2 frames)", f"{v2['blue_paint']['frames']}/30",
                f"+{d['blue_paint']['frame_delta']}")
    col3.metric("Tile (v1 frames)", f"{v1['ceramic_tile_30cm']['frames']}/30")
    col4.metric("Tile (v2 frames)", f"{v2['ceramic_tile_30cm']['frames']}/30",
                f"+{d['ceramic_tile_30cm']['frame_delta']}")

    st.subheader("Detected vs ground truth")
    expected = report["ground_truth_delta"]
    rows = []
    for ch in expected:
        sku = ch["sku"]
        det = d.get(sku, {})
        rows.append({
            "element": ch["element"],
            "type": ch["type"],
            "sku": sku,
            "expected_delta_m2 / count": ch.get("delta_area_m2") or ch.get("delta_count"),
            "detected (crop Δ)": det.get("crop_delta", 0),
            "detected (frame Δ)": det.get("frame_delta", 0),
        })
    st.dataframe(rows, width="stretch")

    st.subheader("Known limitations")
    for lim in report.get("known_limitations", []):
        st.markdown(f"- {lim}")

    st.subheader("Hero comparison")
    hero_left, hero_right = st.columns(2)
    with hero_left:
        st.caption("visit 1 (before) — frame_00015")
        st.image(str(DATA / "visit_1" / "frames" / "frame_00015.jpg"), width="stretch")
    with hero_right:
        st.caption("visit 2 (after) — frame_00015")
        st.image(str(DATA / "visit_2" / "frames" / "frame_00015.jpg"), width="stretch")


def page_upload():
    import subprocess, sys, time, uuid
    st.title("Upload — run pipeline on your own video")
    st.caption(
        "Upload a short video of one of two visits to the same room. "
        "We'll extract frames, segment, and match against the reference SKU library. "
        "Allow ~5–15 minutes (CPU). One job runs at a time."
    )

    jobs_dir = OUTPUTS / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)

    # check if a job is already in flight
    running = []
    for j in jobs_dir.glob("*.json"):
        try:
            d = json.loads(j.read_text())
            if d.get("overall_status") == "running":
                running.append(d)
        except Exception:
            pass

    if running:
        st.warning(f"A job is already running ({running[0]['job_id']}). "
                   f"Wait for it to finish or watch its progress below.")

    with st.form("upload_form", clear_on_submit=False):
        col1, col2 = st.columns(2)
        with col1:
            visit = st.radio("Target visit", [1, 2], horizontal=True,
                              help="visit_1 = before, visit_2 = after")
        with col2:
            fps = st.slider("Frames per second to extract", 1.0, 6.0, 2.0, 0.5)
        video = st.file_uploader("Video", type=["mp4", "mov", "MOV", "MP4", "m4v", "mkv"])
        enable_3d = st.checkbox("启用3D建模", value=False,
                                 help="上传后自动生成3D点云模型（需要open3d）")
        col_btn1, col_btn2 = st.columns(2)
        with col_btn1:
            submitted = st.form_submit_button("Start pipeline", type="primary",
                                              disabled=len(running) > 0)
        with col_btn2:
            submitted_3d = st.form_submit_button("生成3D模型", type="secondary",
                                                  disabled=len(running) > 0 or not enable_3d)

    if submitted or submitted_3d:
        if not video:
            st.error("Please pick a video file.")
        else:
            visit_dir = DATA / f"visit_{visit}"
            visit_dir.mkdir(parents=True, exist_ok=True)
            video_path = visit_dir / f"raw{Path(video.name).suffix.lower()}"
            video_path.write_bytes(video.getbuffer())
            # Wipe stale frame outputs for this visit so we start clean
            frames_dir = visit_dir / "frames"
            if frames_dir.exists():
                for f in frames_dir.glob("frame_*.jpg"):
                    f.unlink()
            seg_dir = OUTPUTS / f"visit_{visit}" / "segments"
            if seg_dir.exists():
                import shutil
                shutil.rmtree(seg_dir)

            job_id = f"job_{beijing_timestamp()}_{uuid.uuid4().hex[:6]}"
            cmd = [
                sys.executable, str(ROOT / "scripts" / "run_pipeline.py"),
                "--job-id", job_id,
                "--visit", str(visit),
                "--video", str(video_path),
                "--root", str(ROOT),
                "--fps", str(fps),
            ]
            # 如果点击了3D建模按钮，添加3D重建参数
            if submitted_3d:
                cmd.extend(["--reconstruct"])
                st.info("已启用3D建模模式，将在处理完成后生成点云模型...")

            # Detached subprocess so streamlit can keep serving
            log = (jobs_dir / f"{job_id}.subprocess.log").open("w")
            subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=True)
            st.session_state["last_job_id"] = job_id
            st.success(f"Started **{job_id}**. Watch progress below.")
            st.rerun()

    st.divider()
    st.subheader("Job history")
    job_files = sorted(jobs_dir.glob("job_*.json"), reverse=True)[:20]
    if not job_files:
        st.info("No jobs yet.")
        return
    for jf in job_files:
        try:
            d = json.loads(jf.read_text())
        except Exception:
            continue
        with st.expander(f"**{d['job_id']}** · visit_{d.get('visit')} · {d.get('overall_status', '?')}",
                         expanded=(d.get("overall_status") == "running"
                                   or d['job_id'] == st.session_state.get("last_job_id", ""))):
            st.caption(f"started: {d.get('started_at', '?')}  ·  updated: {d.get('updated_at', '?')}"
                       + (f"  ·  finished: {d.get('finished_at', '')}" if d.get('finished_at') else ""))
            for s in d.get("stages", []):
                emoji = {"pending": "⚪", "running": "🟡", "done": "✅",
                         "failed": "❌", "skipped": "⏭️"}.get(s["status"], "•")
                msg = f" — {s.get('message','')}" if s.get('message') else ""
                st.markdown(f"{emoji} **{s['name']}** · {s['status']}{msg}")
            log_p = jobs_dir / f"{d['job_id']}.log"
            if log_p.exists():
                with st.expander("Full log"):
                    st.code(log_p.read_text()[-8000:], language="text")
            if d.get("overall_status") == "running":
                st.button("↻ Refresh", key=f"r_{d['job_id']}",
                          on_click=lambda: None, help="Click to refresh status")


def page_visits():
    st.title("Visits — frame viewer")

    # Top: input video playback (built from frames)
    with st.expander("Input video (stitched from frames)", expanded=False):
        vc1, vc2 = st.columns(2)
        v1_mp4 = OUTPUTS / "visit_1_input.mp4"
        v2_mp4 = OUTPUTS / "visit_2_input.mp4"
        if v1_mp4.exists():
            vc1.caption("visit_1")
            vc1.video(str(v1_mp4))
        if v2_mp4.exists():
            vc2.caption("visit_2")
            vc2.video(str(v2_mp4))

    col_top1, col_top2, col_top3 = st.columns([1, 1, 2])
    with col_top1:
        visit = st.radio("Visit", [1, 2], horizontal=True)
    with col_top2:
        overlay = st.toggle("Show segmentation overlay", value=True)
    with col_top3:
        compare = st.toggle("Side-by-side compare with the other visit", value=True)

    frames = list_frames(visit)
    if not frames:
        st.warning(f"No frames in data/visit_{visit}/frames/")
        return
    other = 2 if visit == 1 else 1
    other_frames = list_frames(other)

    idx = st.slider("Frame index", 0, len(frames) - 1, 15)
    fp = frames[idx]
    seg_dir = OUTPUTS / f"visit_{visit}" / "segments"

    if compare and idx < len(other_frames):
        col1, col2 = st.columns(2)
        with col1:
            st.caption(f"visit_{visit} · {fp.name}")
            img = overlay_masks(fp, seg_dir) if overlay and seg_dir.exists() else cv2.cvtColor(cv2.imread(str(fp)), cv2.COLOR_BGR2RGB)
            st.image(img, width="stretch")
        with col2:
            other_fp = other_frames[idx]
            other_seg = OUTPUTS / f"visit_{other}" / "segments"
            st.caption(f"visit_{other} · {other_fp.name}")
            img2 = overlay_masks(other_fp, other_seg) if overlay and other_seg.exists() else cv2.cvtColor(cv2.imread(str(other_fp)), cv2.COLOR_BGR2RGB)
            st.image(img2, width="stretch")
    else:
        img = overlay_masks(fp, seg_dir) if overlay and seg_dir.exists() else cv2.cvtColor(cv2.imread(str(fp)), cv2.COLOR_BGR2RGB)
        st.image(img, width="stretch")

    # Per-frame instance details
    manifest = load_json(seg_dir / "manifest.json")
    if manifest:
        rows = [m for m in manifest if m["frame"] == fp.name]
        if rows:
            st.subheader(f"Detected instances in {fp.name} ({len(rows)})")
            st.dataframe(
                [{"instance_id": r["instance_id"], "class": r["class"], "score": round(r["score"], 3),
                  "area_px": r["area_px"]} for r in rows],
                width="stretch",
            )


def _pointcloud_trace(xyz, rgb, name, opacity=0.85):
    colors = ["rgb(%d,%d,%d)" % (int(r * 255), int(g * 255), int(b * 255)) for r, g, b in rgb]
    return go.Scatter3d(
        x=xyz[:, 0], y=xyz[:, 1], z=xyz[:, 2],
        mode="markers",
        marker=dict(size=2, color=colors, opacity=opacity),
        name=name,
        hovertemplate=f"{name}<br>x=%{{x:.2f}} y=%{{y:.2f}} z=%{{z:.2f}}<extra></extra>",
    )


def _camera_trace(centers, name, color):
    return go.Scatter3d(
        x=centers[:, 0], y=centers[:, 1], z=centers[:, 2],
        mode="markers+lines",
        marker=dict(size=4, color=color, symbol="diamond"),
        line=dict(color=color, width=2),
        name=name,
    )


def page_world_model():
    st.title("World model — sparse 3D reconstruction")
    st.caption(
        "What we have: COLMAP sparse Structure-from-Motion. "
        "A few hundred 3D points + recovered camera poses per visit. "
        "**Not** a textured 3D mesh, **not** a 3DGS/NeRF — those need GPU. "
        "**Not** a generative world model (NVIDIA Cosmos style) — that's a different tool."
    )

    col1, col2, col3 = st.columns(3)
    show_v1 = col1.toggle("Show visit 1 points", value=True)
    show_v2 = col2.toggle("Show visit 2 points", value=True)
    show_cams = col3.toggle("Show ground-truth camera path", value=True)

    traces = []
    summary = []
    if show_v1:
        xyz1, rgb1 = load_largest_pointcloud(1)
        if xyz1 is not None:
            traces.append(_pointcloud_trace(xyz1, rgb1, "visit_1 points"))
            summary.append(("visit_1", len(xyz1)))
    if show_v2:
        xyz2, rgb2 = load_largest_pointcloud(2)
        if xyz2 is not None:
            traces.append(_pointcloud_trace(xyz2, rgb2, "visit_2 points"))
            summary.append(("visit_2", len(xyz2)))
    if show_cams:
        c1 = load_gt_camera_centers(1)
        c2 = load_gt_camera_centers(2)
        if c1 is not None:
            traces.append(_camera_trace(c1, "visit_1 cameras (GT)", "rgb(50,120,200)"))
        if c2 is not None:
            traces.append(_camera_trace(c2, "visit_2 cameras (GT)", "rgb(200,80,80)"))

    if not traces:
        st.warning("No reconstructions found. Run `src/02_reconstruct.py` first.")
        return

    fig = go.Figure(data=traces)
    fig.update_layout(
        height=720,
        scene=dict(aspectmode="data",
                   xaxis_title="X", yaxis_title="Y", zaxis_title="Z"),
        margin=dict(l=0, r=0, t=0, b=0),
        legend=dict(yanchor="top", y=0.98, xanchor="left", x=0.02),
    )
    st.plotly_chart(fig, width="stretch")

    if summary:
        st.markdown("**Sparse-cloud sizes:** " + ", ".join(f"{n}={c} points" for n, c in summary))

    with st.expander("How is this generated?"):
        st.markdown(
            """
- `src/02_reconstruct.py` runs COLMAP: feature extraction → sequential matching → SfM → (best-effort dense MVS).
- Dense MVS step crashes on Mac without CUDA, so we end at sparse reconstruction.
- The resulting `points3D.ply` and `cameras.bin` / `images.bin` define the world frame.
- **Camera poses shown above are the ground-truth poses from `data/visit_*/poses.json`** (we know the synthetic camera path exactly). The COLMAP-recovered poses are in the binary files but in an arbitrary frame.
- For a *textured* 3D model: rent a GPU for an hour, run `gsplat` or COLMAP dense MVS — the same input video gives a photoreal scene.
            """
        )


def page_point_cloud():
    """点云查看器 - 支持加载和显示PLY文件."""
    st.title("Point Cloud Viewer — 点云查看器")
    st.caption("上传或选择PLY点云文件进行3D可视化")

    if not OPEN3D_AVAILABLE:
        st.warning("⚠️ Open3D 未安装。请运行: `pip install open3d` 以启用3D点云功能。")
        return

    # 文件选择方式
    tab1, tab2 = st.tabs(["📁 选择现有文件", "📤 上传PLY文件"])

    pcd = None
    file_name = None

    with tab1:
        # 扫描所有可能的PLY文件位置
        ply_files = []

        # 1. Demo数据目录
        for visit in [1, 2]:
            sparse_dir = OUTPUTS / f"visit_{visit}" / "sparse"
            if sparse_dir.exists():
                for ply in sparse_dir.rglob("*.ply"):
                    ply_files.append((f"Demo visit_{visit}: {ply.name}", ply))

        # 2. 项目目录中的3D模型（从施工照片3D建模生成的）
        # 扫描 data/projects/*/3d_models/
        projects_dir = DATA / "projects"
        if projects_dir.exists():
            for project_dir in projects_dir.iterdir():
                if project_dir.is_dir():
                    models_dir = project_dir / "3d_models"
                    if models_dir.exists():
                        for ply in models_dir.rglob("*.ply"):
                            rel_path = ply.relative_to(models_dir)
                            ply_files.append((f"Project {project_dir.name}: {rel_path}", ply))

        # 3. 全局3D模型目录（兼容旧版本）
        global_models_dir = ROOT / "3d_models"
        if global_models_dir.exists():
            for ply in global_models_dir.rglob("*.ply"):
                rel_path = ply.relative_to(global_models_dir)
                ply_files.append((f"Global: {rel_path}", ply))

        # 4. 用户上传的点云文件
        uploaded_dir = DATA / "pointclouds"
        if uploaded_dir.exists():
            for ply in uploaded_dir.rglob("*.ply"):
                ply_files.append((f"Uploaded: {ply.name}", ply))

        if not ply_files:
            st.info("未找到现有的PLY文件。请先运行3D建模或上传一个PLY文件。")
        else:
            options = [name for name, _ in ply_files]
            selected = st.selectbox("选择点云文件", options)
            if selected:
                file_name = selected
                pcd_path = dict(ply_files)[selected]
                try:
                    pcd = o3d.io.read_point_cloud(str(pcd_path))
                    st.success(f"已加载: {pcd_path}")
                except Exception as e:
                    st.error(f"加载失败: {e}")

    with tab2:
        uploaded = st.file_uploader("上传PLY文件", type=["ply"])
        save_uploaded = st.checkbox("保存到项目目录", value=True,
                                     help="将上传的PLY文件保存到 data/pointclouds/ 目录以便后续查看")
        if uploaded:
            try:
                import tempfile
                with tempfile.NamedTemporaryFile(suffix=".ply", delete=False) as tmp:
                    tmp.write(uploaded.getbuffer())
                    tmp_path = tmp.name
                pcd = o3d.io.read_point_cloud(tmp_path)
                file_name = uploaded.name

                # 保存到项目目录
                if save_uploaded:
                    save_dir = DATA / "pointclouds"
                    save_dir.mkdir(parents=True, exist_ok=True)
                    save_path = save_dir / uploaded.name
                    # 如果文件已存在，添加时间戳
                    if save_path.exists():
                        timestamp = beijing_timestamp()
                        name = uploaded.name.replace(".ply", f"_{timestamp}.ply")
                        save_path = save_dir / name
                        file_name = name
                    import shutil
                    shutil.copy(tmp_path, save_path)
                    st.success(f"已上传并保存: {file_name}")
                    st.caption(f"保存路径: {save_path}")
                else:
                    st.success(f"已上传: {uploaded.name}")
            except Exception as e:
                st.error(f"上传文件读取失败: {e}")

    # 显示点云
    if pcd is not None and len(pcd.points) > 0:
        st.divider()
        st.subheader(f"📊 点云信息: {file_name or 'Unknown'}")

        # 点云统计信息
        xyz = np.asarray(pcd.points)
        n_points = len(xyz)

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("点数", f"{n_points:,}")
        col2.metric("X范围", f"{xyz[:,0].min():.2f} ~ {xyz[:,0].max():.2f}")
        col3.metric("Y范围", f"{xyz[:,1].min():.2f} ~ {xyz[:,1].max():.2f}")
        col4.metric("Z范围", f"{xyz[:,2].min():.2f} ~ {xyz[:,2].max():.2f}")

        # 颜色信息
        has_colors = len(pcd.colors) > 0
        rgb = np.asarray(pcd.colors) if has_colors else np.full((n_points, 3), 0.5)

        # 可视化选项
        st.divider()
        viz_col1, viz_col2, viz_col3 = st.columns(3)
        with viz_col1:
            point_size = st.slider("点大小", 1, 10, 3)
        with viz_col2:
            opacity = st.slider("透明度", 0.1, 1.0, 0.8)
        with viz_col3:
            show_axis = st.checkbox("显示坐标轴", value=True)

        # 3D可视化
        st.subheader("🎯 3D可视化")

        # 采样显示（如果点太多）
        max_display = 50000
        if n_points > max_display:
            st.caption(f"⚠️ 点数过多 ({n_points:,})，显示随机采样的 {max_display:,} 个点")
            indices = np.random.choice(n_points, max_display, replace=False)
            xyz_display = xyz[indices]
            rgb_display = rgb[indices]
        else:
            xyz_display = xyz
            rgb_display = rgb

        # Plotly 3D散点图
        colors = [f"rgb({int(r*255)},{int(g*255)},{int(b*255)})" for r, g, b in rgb_display]

        fig = go.Figure(data=[go.Scatter3d(
            x=xyz_display[:, 0],
            y=xyz_display[:, 1],
            z=xyz_display[:, 2],
            mode='markers',
            marker=dict(
                size=point_size,
                color=colors if has_colors else 'lightgray',
                opacity=opacity
            ),
            text=[f"Point {i}<br>x={x:.3f}<br>y={y:.3f}<br>z={z:.3f}" for i, (x, y, z) in enumerate(xyz_display)],
            hoverinfo='text'
        )])

        # 添加坐标轴
        if show_axis:
            axis_length = np.max(np.abs(xyz)) * 0.3
            fig.add_trace(go.Scatter3d(
                x=[0, axis_length], y=[0, 0], z=[0, 0],
                mode='lines', line=dict(color='red', width=4), name='X轴'
            ))
            fig.add_trace(go.Scatter3d(
                x=[0, 0], y=[0, axis_length], z=[0, 0],
                mode='lines', line=dict(color='green', width=4), name='Y轴'
            ))
            fig.add_trace(go.Scatter3d(
                x=[0, 0], y=[0, 0], z=[0, axis_length],
                mode='lines', line=dict(color='blue', width=4), name='Z轴'
            ))

        fig.update_layout(
            height=700,
            scene=dict(
                aspectmode='data',
                xaxis_title='X',
                yaxis_title='Y',
                zaxis_title='Z',
            ),
            margin=dict(l=0, r=0, t=30, b=0),
            title=dict(text=f"{file_name or 'Point Cloud'} ({n_points:,} points)", x=0.5)
        )

        st.plotly_chart(fig, width="stretch")

        # 下载按钮
        st.divider()
        if st.button("💾 导出当前视图为PNG"):
            st.info("右键点击上方3D图形，选择'Save image as'即可保存图片。")

    elif pcd is not None and len(pcd.points) == 0:
        st.error("❌ 点云文件为空（0个点）")


def page_segments():
    st.title("Segments — instance gallery")
    visit = st.radio("Visit", [1, 2], horizontal=True, key="seg_visit")
    seg_dir = OUTPUTS / f"visit_{visit}" / "segments"
    manifest = load_json(seg_dir / "manifest.json")
    if not manifest:
        st.warning(f"No manifest at {seg_dir / 'manifest.json'}")
        return
    classes = sorted({m["class"].strip() for m in manifest})
    cls_filter = st.multiselect("Filter by class", classes, default=classes)
    score_min = st.slider("Min score", 0.0, 1.0, 0.0, 0.05)

    filtered = [m for m in manifest if m["class"].strip() in cls_filter and m["score"] >= score_min]
    st.markdown(f"**{len(filtered)} / {len(manifest)} instances**")

    page_size = 24
    pages = max(1, (len(filtered) + page_size - 1) // page_size)
    pg = st.number_input("Page", 1, pages, 1)
    page_items = filtered[(pg - 1) * page_size : pg * page_size]
    cols = st.columns(4)
    for i, item in enumerate(page_items):
        crop_path = seg_dir / f"{item['instance_id']}_crop.jpg"
        with cols[i % 4]:
            if crop_path.exists():
                st.image(str(crop_path), caption=f"{item['class']} · {item['score']:.2f}", width="stretch")
            else:
                st.text(item["instance_id"])


def page_matches():
    st.title("Matches — review SKU assignments (HITL)")
    visit = st.radio("Visit", [1, 2], horizontal=True, key="match_visit")
    seg_dir = OUTPUTS / f"visit_{visit}" / "segments"
    matches_path = OUTPUTS / f"visit_{visit}" / "matches_v2.json"
    matches = load_json(matches_path)
    manifest = load_json(seg_dir / "manifest.json")
    if not matches or not manifest:
        st.warning(f"Missing {matches_path} or manifest. Run pipeline.")
        return

    cls_by_inst = {m["instance_id"]: m["class"].strip() for m in manifest}
    skus = sorted({s for m in matches.values() for s in m.get("all_scores", {}).keys()})
    sku_filter = st.multiselect("Filter by matched SKU", skus + ["(none)"], default=skus)

    rows = []
    for rel, m in matches.items():
        iid = rel.replace("_crop.jpg", "")
        cls = cls_by_inst.get(iid, "?")
        match_label = m.get("sku") or "(none)"
        if match_label not in sku_filter:
            continue
        rows.append({"iid": iid, "rel": rel, "cls": cls, "match": m})

    score_min = st.slider("Min top similarity", 0.0, 1.0, 0.0, 0.05, key="match_score_min")
    rows = [r for r in rows if r["match"]["score"] >= score_min]

    st.markdown(f"**{len(rows)} matches**")
    decisions = st.session_state.hitl.setdefault("instance_decisions", {})

    page_size = 12
    pages = max(1, (len(rows) + page_size - 1) // page_size)
    pg = st.number_input("Page", 1, pages, 1, key="match_pg")
    page_rows = rows[(pg - 1) * page_size : pg * page_size]

    for r in page_rows:
        iid = r["iid"]
        cur = decisions.get(iid, {})
        with st.container(border=True):
            cc = st.columns([1, 2, 1])
            crop_path = seg_dir / f"{iid}_crop.jpg"
            if crop_path.exists():
                cc[0].image(str(crop_path), width="stretch")
            cc[1].markdown(f"**{iid}**")
            cc[1].markdown(f"detected class: `{r['cls']}`")
            cc[1].markdown(
                "  ".join(f"{k}=**{v:.2f}**" for k, v in sorted(r["match"]["all_scores"].items(), key=lambda x: -x[1]))
            )
            cc[1].markdown(f"top match: `{r['match']['sku']}` (score={r['match']['score']:.2f})")
            cur_status = cur.get("decision", "—")
            cc[1].markdown(f"current decision: **{cur_status}**" + (f" → `{cur.get('sku')}`" if cur.get("sku") else ""))
            with cc[2]:
                btn1, btn2, btn3 = st.columns(3)
                if btn1.button("✓", key=f"acc_{iid}", help="Accept top match"):
                    set_decision(iid, "accepted", r["match"]["sku"])
                    st.rerun()
                if btn2.button("✗", key=f"rej_{iid}", help="Reject (false positive)"):
                    set_decision(iid, "rejected", None)
                    st.rerun()
                # Manual override dropdown
                override = st.selectbox(
                    "Override SKU", ["(no change)"] + skus,
                    key=f"ov_{iid}", label_visibility="collapsed",
                )
                if override != "(no change)" and st.button("save", key=f"sv_{iid}"):
                    set_decision(iid, "overridden", override)
                    st.rerun()


def page_references():
    st.title("Reference SKU library")
    refs_dir = DATA / "references"
    if not refs_dir.exists():
        st.warning("No data/references/ directory.")
        return
    sku_dirs = sorted([d for d in refs_dir.iterdir() if d.is_dir()])
    for skud in sku_dirs:
        with st.expander(f"**{skud.name}** ({len(list(skud.glob('*')))} images)", expanded=True):
            imgs = sorted(p for p in skud.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png"})
            cols = st.columns(min(6, max(1, len(imgs))))
            for i, ip in enumerate(imgs):
                cols[i % len(cols)].image(str(ip), caption=ip.name, width="stretch")


def page_diff():
    st.title("Change report — visit_1 → visit_2")
    expected = load_json(DATA / "expected_diff.json")
    report = load_json(OUTPUTS / "smoke_test_report.json")
    if not report:
        st.warning("Run the pipeline to produce outputs/smoke_test_report.json")
        return

    decisions = st.session_state.hitl.setdefault("change_decisions", {})

    for i, ch in enumerate(expected.get("expected_changes", [])):
        sku = ch["sku"]
        det = report["detected_changes"].get(sku, {})
        with st.container(border=True):
            c1, c2, c3 = st.columns([2, 2, 1])
            c1.markdown(f"### {ch['type']} — `{ch['element']}`")
            c1.markdown(f"SKU: `{sku}`")
            c1.markdown(
                f"**Expected:** {ch.get('delta_count', '')} {'tiles' if 'count' in ch else ''} "
                f"({ch.get('delta_area_m2', '')} m²)"
            )
            c1.markdown(
                f"**Detected:** crop Δ = {det.get('crop_delta', 0)}, "
                f"frame Δ = {det.get('frame_delta', 0)} / 30"
            )
            cur = decisions.get(str(i), "—")
            c2.markdown(f"**HITL decision:** {cur}")
            with c3:
                if st.button("Confirm", key=f"conf_{i}"):
                    decisions[str(i)] = "confirmed"
                    save_hitl(st.session_state.hitl)
                    st.rerun()
                if st.button("Dismiss", key=f"dis_{i}"):
                    decisions[str(i)] = "dismissed"
                    save_hitl(st.session_state.hitl)
                    st.rerun()

    st.subheader("Raw smoke-test report")
    st.json(report)


# ============ dispatch ============
PAGES = {
    # New multi-tenant
    "Projects": view_projects,
    "Tasks": lambda: view_task_tracking(get_current_project()),
    "图纸分析": lambda: view_drawing_analysis(get_current_project()),
    "DWG转PDF": lambda: view_dwg2pdf(get_current_project()),
    "人工审核": lambda: view_manual_review(get_current_project()),
    "审图知识库": view_review_knowledge_base,
    "Profile": view_profile,
    "Users": view_admin_users,
    "LLM 设置": view_llm_settings,
    # Knowledge Base
    "知识库": view_knowledge_base,
    "智能问答": view_chatbot,
    "图片问答": view_chat_image,
    # Demo data (preserved as a reference of what the pipeline produces)
    "Overview": page_overview,
    "Upload": page_upload,
    "Visits": page_visits,
    "World model": page_world_model,
    "Point Cloud": page_point_cloud,
    "Segments": page_segments,
    "Matches (HITL)": page_matches,
    "References": page_references,
    "Change report": page_diff,
}
if page in PAGES:
    PAGES[page]()
else:
    # group label was accidentally selected — fall through
    view_projects()
