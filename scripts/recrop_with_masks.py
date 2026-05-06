"""Re-crop each segmentation instance using its mask (instead of just its bbox).

Usage:
    python scripts/recrop_with_masks.py <segments_dir> <frames_dir>
"""
from __future__ import annotations
from pathlib import Path
import sys

import cv2
import numpy as np
from tqdm import tqdm
import typer

app = typer.Typer(add_completion=False)


@app.command()
def main(
    segments_dir: Path = typer.Argument(..., exists=True, file_okay=False),
    frames_dir: Path = typer.Argument(..., exists=True, file_okay=False),
):
    pairs = sorted(segments_dir.glob("*/*_mask.png"))
    print(f"Re-cropping {len(pairs)} instances using their masks...")
    for mask_path in tqdm(pairs):
        frame_stem = mask_path.parent.name
        crop_path = mask_path.with_name(mask_path.name.replace("_mask.png", "_crop.jpg"))
        if not crop_path.exists():
            continue

        full_frame = cv2.imread(str(frames_dir / f"{frame_stem}.jpg"))
        mask = cv2.imread(str(mask_path), cv2.IMREAD_GRAYSCALE)
        if full_frame is None or mask is None:
            continue
        if mask.shape[:2] != full_frame.shape[:2]:
            mask = cv2.resize(mask, (full_frame.shape[1], full_frame.shape[0]),
                              interpolation=cv2.INTER_NEAREST)

        bool_mask = mask > 127
        if bool_mask.sum() < 100:
            continue
        out = np.full_like(full_frame, 128)
        out[bool_mask] = full_frame[bool_mask]

        ys, xs = np.where(bool_mask)
        y0, y1 = ys.min(), ys.max() + 1
        x0, x1 = xs.min(), xs.max() + 1
        cropped = out[y0:y1, x0:x1]
        cv2.imwrite(str(crop_path), cropped, [cv2.IMWRITE_JPEG_QUALITY, 92])

    print("Done.")


if __name__ == "__main__":
    app()
