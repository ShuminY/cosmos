"""Pluggable document analyzers.

Each analyzer is a function `analyze(path: Path) -> dict` returning a JSON-able
dict. The dispatcher picks one based on file extension.

Add a new format: register a new function in REGISTRY by extension.
"""
from __future__ import annotations
from pathlib import Path
import json
import traceback

from .text_docs import analyze_pdf, analyze_docx, analyze_pptx
from .spreadsheet import analyze_xlsx
from .bim import analyze_ifc
from .media import analyze_image, analyze_video

# extension -> (analyzer fn, status_when_unsupported)
REGISTRY = {
    ".pdf":  analyze_pdf,
    ".docx": analyze_docx,
    ".doc":  analyze_docx,    # python-docx supports .docx; .doc may fail — tagged unsupported by analyzer if so
    ".pptx": analyze_pptx,
    ".ppt":  analyze_pptx,
    ".xlsx": analyze_xlsx,
    ".xls":  analyze_xlsx,
    ".csv":  analyze_xlsx,
    ".ifc":  analyze_ifc,
    ".jpg":  analyze_image,
    ".jpeg": analyze_image,
    ".png":  analyze_image,
    ".heic": analyze_image,
    ".mp4":  analyze_video,
    ".mov":  analyze_video,
    ".m4v":  analyze_video,
    ".mkv":  analyze_video,
}

UNSUPPORTED_NOTE = {
    ".dwg":  "DWG: needs ODA File Converter or convert to DXF first.",
    ".dxf":  "DXF: parser not yet wired (use ezdxf).",
    ".rvt":  "Revit native format — export to IFC or NWC for analysis.",
    ".rfa":  "Revit family — export to IFC for analysis.",
    ".skp":  "SketchUp native format — export to IFC or DAE.",
    ".3dm":  "Rhino — needs rhino3dm parser (not yet wired).",
    ".mpp":  "MS Project — export to XML or CSV first.",
}


def run_analyzer(path: Path) -> dict:
    """Returns a dict with at least {status, summary, data?, error?}."""
    ext = path.suffix.lower()
    if ext in REGISTRY:
        try:
            data = REGISTRY[ext](path)
            return {
                "status": "done",
                "summary": data.get("summary", ""),
                "data": data,
            }
        except Exception as e:
            return {
                "status": "failed",
                "summary": f"analyzer crashed: {type(e).__name__}",
                "error": f"{type(e).__name__}: {e}\n{traceback.format_exc()[:1000]}",
            }
    if ext in UNSUPPORTED_NOTE:
        return {
            "status": "unsupported",
            "summary": UNSUPPORTED_NOTE[ext],
        }
    return {
        "status": "unsupported",
        "summary": f"No analyzer registered for {ext}",
    }


def serialize(result: dict) -> str:
    """JSON-encode the analyzer payload (safe for SQLite TEXT column)."""
    return json.dumps(result, ensure_ascii=False, default=str)
