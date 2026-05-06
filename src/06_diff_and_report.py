"""Compute change metrics between visit_1 and visit_2, write JSON + markdown report.

Inputs:
    - aligned point clouds (from 03_align.py)
    - per-frame segmentation manifests (from 04_segment.py) for both visits
    - reference matches (from 05_match_references.py) for visit_2

Outputs:
    outputs/report.json    — structured per-element diff
    outputs/report.md      — human-readable summary

Metrics:
    - per-wall:  m² of newly painted area (color delta over masked surface)
    - per-floor: count of newly laid tile instances
    - per-door:  installed / not yet
    - global:    bounding-box volume change, point count delta

Skeleton — wires the pieces together; metric implementations are TODOs.
"""
from __future__ import annotations
from pathlib import Path
import json
import typer

app = typer.Typer(add_completion=False)


@app.command()
def main(
    visit_1: Path = typer.Argument(..., exists=True, file_okay=False),
    visit_2: Path = typer.Argument(..., exists=True, file_okay=False),
    report_md: Path = typer.Argument(...),
):
    report_md.parent.mkdir(parents=True, exist_ok=True)
    report_json = report_md.with_suffix(".json")

    # TODO: load aligned clouds; restrict each segmented mask to its 3D surface
    # TODO: paint diff — for each wall surface, compare median color in both visits
    # TODO: tile count — instance segment + count tiles, diff
    # TODO: write report

    report = {
        "visit_1": str(visit_1),
        "visit_2": str(visit_2),
        "elements": [],   # [{id, type, sku, change_type, quantity, unit}]
    }
    json.dump(report, open(report_json, "w"), indent=2)
    report_md.write_text("# Change report\n\nTODO: fill in once metrics are implemented.\n")
    print(f"Wrote {report_json} and {report_md}")


if __name__ == "__main__":
    app()
