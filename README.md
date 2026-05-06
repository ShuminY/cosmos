# cosmos — construction progress monitoring POC

Reconstruct rooms from images/video, then detect and quantify changes between visits
(e.g. "wall_3 is 78% painted (+12 m²)", "47 ceramic tiles laid in floor_1").

## Pipeline

```
video/images (visit_N)
    │
    ▼  01_extract_frames.py        (ffmpeg)
frames/
    │
    ▼  02_reconstruct.py           (COLMAP — SfM + dense MVS)
sparse + dense point cloud + camera poses
    │
    ▼  03_align.py                 (Open3D — register visit_2 to visit_1)
aligned scans in shared coord frame
    │
    ▼  04_segment.py               (Grounded-SAM-2 — wall/floor/door/tile masks)
per-frame 2D masks, lifted to 3D via camera poses
    │
    ▼  05_match_references.py      (DINOv2 — match user-supplied refs)
labeled 3D segments
    │
    ▼  06_diff_and_report.py       (geometry + texture diff)
JSON change report + markdown summary
```

## Layout

| dir | purpose |
|---|---|
| `data/visit_1/` `data/visit_2/` | raw video / image folders, one subdir per scan |
| `data/references/` | user-supplied reference images of specific tile/door/wall SKUs |
| `src/` | pipeline scripts (numbered, runnable independently) |
| `models/` | downloaded model weights (gitignored) |
| `outputs/` | reconstructions, masks, reports (gitignored) |
| `notebooks/` | exploratory analysis |

## Hardware constraint

Currently targeting **macOS Apple Silicon (no CUDA)**. This rules out `gsplat` /
Nerfstudio's training paths. We use COLMAP for reconstruction (CPU, robust) and
MPS-friendly models for segmentation/embedding. 3DGS is deferred to a cloud-GPU
phase if/when the COLMAP-based POC validates the concept.

## Setup

```bash
brew install ffmpeg colmap uv      # system tools
uv venv --python 3.11 .venv        # python env
source .venv/bin/activate
uv pip install -e .                # install deps from pyproject.toml
```

## Usage (per visit)

```bash
# Visit 1
python src/01_extract_frames.py data/visit_1/raw.mp4 data/visit_1/frames/
python src/02_reconstruct.py    data/visit_1/frames/ outputs/visit_1/

# Visit 2 (later)
python src/01_extract_frames.py data/visit_2/raw.mp4 data/visit_2/frames/
python src/02_reconstruct.py    data/visit_2/frames/ outputs/visit_2/

# Diff
python src/03_align.py     outputs/visit_1 outputs/visit_2
python src/04_segment.py   outputs/visit_2
python src/05_match_references.py outputs/visit_2 data/references/
python src/06_diff_and_report.py outputs/visit_1 outputs/visit_2 outputs/report.md
```
