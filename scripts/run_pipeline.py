"""Project-scoped pipeline orchestrator.

Resolves all paths from the DB (project + capture IDs) — no more hardcoded
data/visit_*. Tracks per-stage status in outputs/jobs/<job_id>.json.

Usage:
    python scripts/run_pipeline.py --job-id <id> --project-id <pid> --capture-id <cid> \
        [--video <path>] [--fps 2.0]

If --video is given, frames are extracted from it. Otherwise we expect the
capture's frames_dir to already exist (e.g., legacy demo data).
"""
from __future__ import annotations
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.db import session, init_db, Project, Capture, Job
from src.storage import (
    project_dir, capture_dir, materials_dir, jobs_dir, slugify,
)


STAGES = ["extract_frames", "segment", "recrop_masks", "match_references", "summarize"]


def update_status(status_path: Path, **kwargs):
    cur = json.loads(status_path.read_text()) if status_path.exists() else {}
    cur.update(kwargs)
    cur["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tmp = status_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cur, indent=2))
    tmp.replace(status_path)


def run_stage(name: str, cmd: list[str], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as f:
        f.write(f"\n\n===== {name} =====\n$ {' '.join(cmd)}\n"); f.flush()
        proc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)
    return proc.returncode


def resolve_paths(project_id: int, capture_id: int) -> dict:
    """Return absolute paths used by the pipeline for this capture."""
    init_db()
    with session() as s:
        p = s.get(Project, project_id)
        c = s.get(Capture, capture_id)
        if not p or not c:
            raise ValueError(f"project {project_id} or capture {capture_id} not found")
        if c.project_id != project_id:
            raise ValueError(f"capture {capture_id} does not belong to project {project_id}")
        # paths stored in DB are relative to project_dir; if missing, use canonical layout
        proot = project_dir(project_id)
        if c.frames_dir:
            frames = (proot / c.frames_dir).resolve()
        else:
            frames = capture_dir(project_id, capture_id) / "frames"
        if c.outputs_dir:
            outputs = (proot / c.outputs_dir).resolve()
        else:
            outputs = capture_dir(project_id, capture_id) / "outputs"
        if c.src_video_path:
            video = (proot / c.src_video_path).resolve()
        else:
            video = None
    return {
        "project_root": proot,
        "frames": frames,
        "outputs": outputs,
        "video": video,
        "segments": outputs / "segments",
        "matches_path": outputs / "matches.json",
        "summary_path": outputs / "summary.json",
        "materials_root": materials_dir(project_id),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--job-id", required=True)
    p.add_argument("--project-id", type=int, required=True)
    p.add_argument("--capture-id", type=int, required=True)
    p.add_argument("--video", type=Path, default=None,
                   help="Optional override video path (otherwise uses capture.src_video_path)")
    p.add_argument("--fps", type=float, default=2.0)
    args = p.parse_args()

    init_db()
    paths = resolve_paths(args.project_id, args.capture_id)
    job_id = args.job_id
    log_path = jobs_dir(args.project_id) / f"{job_id}.log"
    status_path = jobs_dir(args.project_id) / f"{job_id}.json"

    update_status(status_path,
                  job_id=job_id,
                  project_id=args.project_id, capture_id=args.capture_id,
                  stages=[{"name": s, "status": "pending"} for s in STAGES],
                  overall_status="running",
                  started_at=time.strftime("%Y-%m-%d %H:%M:%S"))

    def mark(stage, status, msg=""):
        cur = json.loads(status_path.read_text())
        for s in cur["stages"]:
            if s["name"] == stage:
                s["status"] = status
                if msg: s["message"] = msg
        update_status(status_path, stages=cur["stages"])

    # also update Capture.status in DB
    def set_cap_status(st: str):
        with session() as ses:
            c = ses.get(Capture, args.capture_id)
            if c:
                c.status = st
                ses.add(c)

    set_cap_status("processing")
    py = sys.executable

    try:
        # 1) extract_frames (skip if frames already exist)
        n_existing = len(list(paths["frames"].glob("*.jpg"))) if paths["frames"].exists() else 0
        video = args.video or paths["video"]
        if n_existing > 0 and not args.video:
            mark("extract_frames", "skipped", f"{n_existing} frames already on disk")
        else:
            if not video or not Path(video).exists():
                mark("extract_frames", "failed", "no video provided and no frames on disk")
                update_status(status_path, overall_status="failed")
                set_cap_status("failed")
                return 1
            mark("extract_frames", "running")
            paths["frames"].mkdir(parents=True, exist_ok=True)
            rc = run_stage("extract_frames", [
                py, str(ROOT / "src" / "01_extract_frames.py"),
                str(video), str(paths["frames"]),
                "--fps", str(args.fps),
            ], log_path)
            if rc != 0:
                mark("extract_frames", "failed", f"exit={rc}")
                update_status(status_path, overall_status="failed"); set_cap_status("failed"); return 1
            n_frames = len(list(paths["frames"].glob("*.jpg")))
            mark("extract_frames", "done", f"{n_frames} frames")
            with session() as ses:
                c = ses.get(Capture, args.capture_id)
                c.frames_count = n_frames
                ses.add(c)

        # 2) segment
        mark("segment", "running")
        rc = run_stage("segment", [
            py, str(ROOT / "src" / "04_segment.py"),
            str(paths["frames"]), str(paths["segments"]),
            "--box-threshold", "0.25", "--text-threshold", "0.20",
        ], log_path)
        if rc != 0:
            mark("segment", "failed", f"exit={rc}")
            update_status(status_path, overall_status="failed"); set_cap_status("failed"); return 1
        manifest = paths["segments"] / "manifest.json"
        n_inst = len(json.loads(manifest.read_text())) if manifest.exists() else 0
        mark("segment", "done", f"{n_inst} instances")

        # 3) recrop with masks
        mark("recrop_masks", "running")
        rc = run_stage("recrop_masks", [
            py, str(ROOT / "scripts" / "recrop_with_masks.py"),
            str(paths["segments"]), str(paths["frames"]),
        ], log_path)
        if rc != 0:
            mark("recrop_masks", "failed", f"exit={rc}")
            update_status(status_path, overall_status="failed"); set_cap_status("failed"); return 1
        mark("recrop_masks", "done")

        # 4) match references — only if project's materials dir has SKUs
        sku_dirs = [d for d in paths["materials_root"].iterdir() if d.is_dir()] if paths["materials_root"].exists() else []
        if sku_dirs:
            mark("match_references", "running")
            rc = run_stage("match_references", [
                py, str(ROOT / "src" / "05_match_references.py"),
                str(paths["segments"]), str(paths["materials_root"]),
                str(paths["matches_path"]), "--threshold", "0.50",
            ], log_path)
            if rc != 0:
                mark("match_references", "failed", f"exit={rc}")
                update_status(status_path, overall_status="failed"); set_cap_status("failed"); return 1
            n_matches = len(json.loads(paths["matches_path"].read_text())) if paths["matches_path"].exists() else 0
            mark("match_references", "done", f"{n_matches} matches across {len(sku_dirs)} SKUs")
        else:
            mark("match_references", "skipped", "no materials in project library")

        # 5) summarize
        mark("summarize", "running")
        from collections import Counter
        summary = {
            "project_id": args.project_id,
            "capture_id": args.capture_id,
            "frames": len(list(paths["frames"].glob("*.jpg"))),
            "segmented_instances": n_inst,
        }
        if paths["matches_path"].exists():
            m = json.loads(paths["matches_path"].read_text())
            summary["matches_by_sku"] = dict(Counter(v["sku"] or "(none)" for v in m.values()))
        paths["summary_path"].parent.mkdir(parents=True, exist_ok=True)
        paths["summary_path"].write_text(json.dumps(summary, indent=2))
        mark("summarize", "done")

        update_status(status_path, overall_status="done",
                      finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        set_cap_status("done")
        return 0
    except Exception as e:
        update_status(status_path, overall_status="failed", error=str(e))
        set_cap_status("failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
