#!/usr/bin/env python3
"""3D重建任务脚本 - 处理视频/图片生成点云.

Usage:
    python scripts/run_3d_reconstruction.py --job-id <id> --video <path> \
        --frames-dir <path> --output-dir <path> --status-path <path>
"""
from __future__ import annotations
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


from src.time_utils import beijing_timestamp


def update_status(status_path: Path, **kwargs):
    """更新状态文件."""
    if status_path.exists():
        cur = json.loads(status_path.read_text())
    else:
        cur = {}
    cur.update(kwargs)
    cur["updated_at"] = beijing_timestamp("%Y-%m-%d %H:%M:%S")
    status_path.write_text(json.dumps(cur, indent=2))


def run_stage(name: str, cmd: list[str], log_path: Path) -> int:
    """运行一个阶段并记录日志."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as f:
        f.write(f"\n\n===== {name} =====\n$ {' '.join(cmd)}\n")
        f.flush()
        proc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)
    return proc.returncode


def mark(status_path: Path, stage: str, status: str, msg: str = ""):
    """标记阶段状态."""
    if not status_path.exists():
        return
    cur = json.loads(status_path.read_text())
    for s in cur.get("stages", []):
        if s["name"] == stage:
            s["status"] = status
            if msg:
                s["message"] = msg
    update_status(status_path, stages=cur["stages"])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--job-id", required=True)
    p.add_argument("--video", required=True, type=Path)
    p.add_argument("--frames-dir", required=True, type=Path)
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--status-path", required=True, type=Path)
    p.add_argument("--fps", type=float, default=2.0)
    args = p.parse_args()

    job_id = args.job_id
    log_path = args.status_path.parent / f"{job_id}.log"
    status_path = args.status_path

    py = sys.executable

    try:
        # 检查是否已经有帧存在（图片模式）
        existing_frames = list(args.frames_dir.glob("frame_*.jpg"))
        if existing_frames:
            # 图片模式：帧已存在，跳过提取
            mark(status_path, "extract_frames", "skipped", f"{len(existing_frames)} frames already exist")
            n_frames = len(existing_frames)
        else:
            # 视频模式：需要提取帧
            mark(status_path, "extract_frames", "running")
            args.frames_dir.mkdir(parents=True, exist_ok=True)
            rc = run_stage("extract_frames", [
                py, str(ROOT / "src" / "01_extract_frames.py"),
                str(args.video), str(args.frames_dir),
                "--fps", str(args.fps),
            ], log_path)
            if rc != 0:
                mark(status_path, "extract_frames", "failed", f"exit={rc}")
                update_status(status_path, overall_status="failed",
                             finished_at=beijing_timestamp("%Y-%m-%d %H:%M:%S"))
                return 1

            n_frames = len(list(args.frames_dir.glob("frame_*.jpg")))
            mark(status_path, "extract_frames", "done", f"{n_frames} frames")

        # 2) 3D重建
        mark(status_path, "reconstruct", "running")
        sparse_dir = args.output_dir / "sparse"
        rc = run_stage("reconstruct", [
            py, str(ROOT / "src" / "02_reconstruct.py"),
            str(args.frames_dir), str(sparse_dir),
        ], log_path)
        if rc != 0:
            mark(status_path, "reconstruct", "failed", f"exit={rc}")
            update_status(status_path, overall_status="failed",
                         finished_at=beijing_timestamp("%Y-%m-%d %H:%M:%S"))
            return 1

        # 检查点云文件是否生成
        pcd_file = sparse_dir / "0" / "points3D.ply"
        if pcd_file.exists():
            mark(status_path, "reconstruct", "done", f"point cloud saved to {pcd_file}")
        else:
            mark(status_path, "reconstruct", "failed", "point cloud file not found")
            update_status(status_path, overall_status="failed",
                         finished_at=beijing_timestamp("%Y-%m-%d %H:%M:%S"))
            return 1

        update_status(status_path, overall_status="done",
                     finished_at=beijing_timestamp("%Y-%m-%d %H:%M:%S"))
        return 0

    except Exception as e:
        update_status(status_path, overall_status="failed", error=str(e),
                     finished_at=beijing_timestamp("%Y-%m-%d %H:%M:%S"))
        return 1


if __name__ == "__main__":
    sys.exit(main())
