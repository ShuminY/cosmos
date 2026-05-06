"""Filesystem layout for project data — paths derived from project IDs.

    data/
      projects/
        <project_id>/
          documents/<category>/<filename>
          materials/<sku_slug>/images/*.jpg     # slug = slugified sku_name; matchers read this
          captures/<capture_id>/{raw.mp4, frames/, outputs/}
          analyses/<analysis_id>/
          jobs/

The DB stores RELATIVE paths to keep things portable.
"""
from __future__ import annotations
import re
from pathlib import Path

from .db import DATA_ROOT


def slugify(name: str) -> str:
    """Make a safe directory name from a SKU/material name.

    Lowercased, alphanumeric + underscore + dash; collapses runs; trims to 64 chars.
    Non-ASCII runs collapse to underscore so Chinese names still produce a stable
    slug (deduped via the material id if needed by the caller).
    """
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9一-鿿]+", "_", s)  # keep ASCII + CJK
    s = re.sub(r"_+", "_", s).strip("_-")
    return (s or "material")[:64]


def project_dir(project_id: int) -> Path:
    p = DATA_ROOT / "projects" / str(project_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def documents_dir(project_id: int, category: str) -> Path:
    p = project_dir(project_id) / "documents" / category
    p.mkdir(parents=True, exist_ok=True)
    return p


def materials_dir(project_id: int) -> Path:
    p = project_dir(project_id) / "materials"
    p.mkdir(parents=True, exist_ok=True)
    return p


def material_dir(project_id: int, material_id_or_slug) -> Path:
    """Backward-compat: if an int, fall back to id; otherwise treat as slug."""
    p = materials_dir(project_id) / str(material_id_or_slug)
    (p / "images").mkdir(parents=True, exist_ok=True)
    return p


def material_slug_dir(project_id: int, slug: str) -> Path:
    p = materials_dir(project_id) / slug
    (p / "images").mkdir(parents=True, exist_ok=True)
    return p


def captures_dir(project_id: int) -> Path:
    p = project_dir(project_id) / "captures"
    p.mkdir(parents=True, exist_ok=True)
    return p


def capture_dir(project_id: int, capture_id: int) -> Path:
    p = captures_dir(project_id) / str(capture_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def analyses_dir(project_id: int) -> Path:
    p = project_dir(project_id) / "analyses"
    p.mkdir(parents=True, exist_ok=True)
    return p


def jobs_dir(project_id: int | None = None) -> Path:
    if project_id is None:
        p = DATA_ROOT / "jobs"
    else:
        p = project_dir(project_id) / "jobs"
    p.mkdir(parents=True, exist_ok=True)
    return p
