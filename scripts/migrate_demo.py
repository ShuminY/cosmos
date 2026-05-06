"""One-shot migration: seed a 'Demo Project' that points at the existing
synthetic data (data/visit_1, data/visit_2, data/references) so the new
project-scoped UI/pipeline can operate on it without moving files.

Idempotent: running twice is safe (skips records that already exist by name).

Usage:
    python scripts/migrate_demo.py
"""
from __future__ import annotations
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from src.db import init_db, session, Project, Capture, Material, ProjectMember
from src.auth import ensure_default_admin
from src.storage import project_dir, materials_dir, slugify


def main():
    init_db()
    admin, _ = ensure_default_admin()
    print(f"admin: id={admin.id} email={admin.email}")

    # Find / create demo project
    with session() as s:
        p = s.execute(select(Project).where(Project.name == "Demo Project")).scalar_one_or_none()
        if p is None:
            p = Project(
                name="Demo Project",
                address="Synthetic test data",
                description=(
                    "Auto-migrated synthetic dataset: two captures (before/after), "
                    "ArUco fiducial for metric scale, blue paint and ceramic tile reference SKUs. "
                    "See src/02_reconstruct.py and ground_truth.json for spec."
                ),
                status="completed",
                owner_id=admin.id,
            )
            s.add(p); s.flush()
            print(f"created Demo Project id={p.id}")
        pid = p.id
        proj_root = project_dir(pid)

    # Wipe stale captures/materials from prior migration runs so we can rebuild
    # with the (correct) symlink-based layout. Documents are NOT touched.
    with session() as s:
        for old in list(s.execute(select(Capture).where(Capture.project_id == pid)).scalars()):
            s.delete(old)
        for old in list(s.execute(select(Material).where(Material.project_id == pid)).scalars()):
            s.delete(old)
    # Clean up canonical capture/material dirs — but leave legacy data alone
    cap_root = proj_root / "captures"
    if cap_root.exists():
        shutil.rmtree(cap_root)
    mat_root = proj_root / "materials"
    if mat_root.exists():
        shutil.rmtree(mat_root)

    # Set up captures pointing at legacy data via SYMLINKS into the canonical
    # project layout. DB stores paths relative to project_dir.
    legacy_specs = [
        ("Visit 1 (before)",  "data/visit_1/raw.mp4", "data/visit_1/frames", "outputs/visit_1"),
        ("Visit 2 (after)",   "data/visit_2/raw.mp4", "data/visit_2/frames", "outputs/visit_2"),
    ]

    for name, video_rel, frames_rel, outputs_rel in legacy_specs:
        video_abs = (ROOT / video_rel).resolve()
        frames_abs = (ROOT / frames_rel).resolve()
        outputs_abs = (ROOT / outputs_rel).resolve()
        n_frames = len(list(frames_abs.glob("*.jpg"))) if frames_abs.exists() else 0
        # Insert capture row first to get an ID
        with session() as s:
            c = Capture(
                project_id=pid, name=name,
                captured_at=datetime.utcnow(),
                captured_by_id=admin.id,
                status="done" if outputs_abs.exists() else "uploaded",
                frames_count=n_frames,
                notes="auto-migrated from synthetic dataset",
            )
            s.add(c); s.flush()
            cid = c.id
        # Build canonical capture dir + symlinks pointing at legacy locations
        cdir = proj_root / "captures" / str(cid)
        cdir.mkdir(parents=True, exist_ok=True)
        rel_paths = {}
        if video_abs.exists():
            link = cdir / f"raw{video_abs.suffix}"
            if not link.exists():
                link.symlink_to(video_abs)
            rel_paths["src_video_path"] = str(link.relative_to(proj_root))
        if frames_abs.exists():
            link = cdir / "frames"
            if not link.exists():
                link.symlink_to(frames_abs)
            rel_paths["frames_dir"] = str(link.relative_to(proj_root))
        if outputs_abs.exists():
            link = cdir / "outputs"
            if not link.exists():
                link.symlink_to(outputs_abs)
            rel_paths["outputs_dir"] = str(link.relative_to(proj_root))
        # Update DB with the canonical (project_dir-relative) paths
        with session() as s:
            c = s.get(Capture, cid)
            for k, v in rel_paths.items():
                setattr(c, k, v)
            s.add(c)
        print(f"  capture #{cid} {name!r} · frames={n_frames} · symlinks: {list(rel_paths.keys())}")

    # Migrate references → materials. Each subdir of data/references/ becomes a Material
    # whose images_dir is materials/<slug>/images. Files are MOVED so the new pipeline
    # can read from the canonical location.
    refs_root = ROOT / "data" / "references"
    if refs_root.exists():
        for skud in sorted(refs_root.iterdir()):
            if not skud.is_dir():
                continue
            sku_name = skud.name
            slug = slugify(sku_name)
            # Heuristic type from name
            mtype = "tile" if "tile" in sku_name.lower() else \
                    "paint" if "paint" in sku_name.lower() else "other"
            with session() as s:
                existing = s.execute(
                    select(Material).where(Material.project_id == pid, Material.sku_name == sku_name)
                ).scalar_one_or_none()
                if existing:
                    print(f"skip material {sku_name!r} (already exists, id={existing.id})")
                    continue
                images_dir_rel = f"materials/{slug}/images"
                m = Material(
                    project_id=pid, sku_name=sku_name, type=mtype,
                    images_dir=images_dir_rel, notes="auto-migrated",
                )
                s.add(m); s.flush()
                mid = m.id
            target_images = proj_root / "materials" / slug / "images"
            target_images.mkdir(parents=True, exist_ok=True)
            n_copied = 0
            for img in skud.iterdir():
                if img.is_file() and img.suffix.lower() in {".jpg", ".jpeg", ".png"}:
                    dest = target_images / img.name
                    if not dest.exists():
                        shutil.copy2(img, dest)
                        n_copied += 1
            print(f"material #{mid} {sku_name!r} (slug={slug}, type={mtype}): copied {n_copied} images -> {target_images}")

    print("\nMigration done.")


if __name__ == "__main__":
    main()
