"""Segment building elements (wall/floor/door/tile/...) per-frame using
Grounded-SAM-2 (Grounding DINO + SAM 2) — both via the transformers library.

Usage:
    python src/04_segment.py <frames_dir> <out_dir> [--prompt "wall. floor. tile."]

On Apple Silicon this runs on MPS — slow but functional. First run downloads
~850 MB of model weights to ~/.cache/huggingface/.
"""
from __future__ import annotations
from pathlib import Path
import json
import sys

import numpy as np
import torch
from PIL import Image
from tqdm import tqdm
import typer
from transformers import (
    AutoProcessor,
    AutoModelForZeroShotObjectDetection,
    Sam2Model,
    Sam2Processor,
)

# Allow `python src/04_segment.py` to find the sibling config module
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import ELEMENT_PROMPTS, SAM2_HF, GROUNDING_DINO_HF, get_device

app = typer.Typer(add_completion=False)


def load_models(device: str):
    print(f"Loading Grounding DINO ({GROUNDING_DINO_HF})...")
    gd_proc = AutoProcessor.from_pretrained(GROUNDING_DINO_HF)
    gd_model = AutoModelForZeroShotObjectDetection.from_pretrained(GROUNDING_DINO_HF).to(device).eval()

    print(f"Loading SAM 2 ({SAM2_HF})...")
    sam_proc = Sam2Processor.from_pretrained(SAM2_HF)
    sam_model = Sam2Model.from_pretrained(SAM2_HF).to(device).eval()

    return gd_proc, gd_model, sam_proc, sam_model


@app.command()
def main(
    frames: Path = typer.Argument(..., exists=True, file_okay=False),
    out_dir: Path = typer.Argument(...),
    prompt: str = typer.Option(None, help="Override default element prompts. Period-separated."),
    box_threshold: float = typer.Option(0.30),
    text_threshold: float = typer.Option(0.25),
    save_crops: bool = typer.Option(True, help="Save per-instance crops for reference matching"),
):
    out_dir.mkdir(parents=True, exist_ok=True)
    device = get_device()
    prompts = prompt.split(".") if prompt else ELEMENT_PROMPTS
    prompts = [p.strip().lower() for p in prompts if p.strip()]
    text_query = ". ".join(prompts) + "."
    print(f"device={device}  prompts={prompts}")

    gd_proc, gd_model, sam_proc, sam_model = load_models(device)
    manifest: list[dict] = []

    frame_paths = sorted(p for p in frames.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png"})
    for fp in tqdm(frame_paths, desc="segment"):
        image = Image.open(fp).convert("RGB")

        # 1) Grounding DINO -> boxes
        gd_inputs = gd_proc(images=image, text=text_query, return_tensors="pt").to(device)
        with torch.no_grad():
            gd_out = gd_model(**gd_inputs)
        gd_results = gd_proc.post_process_grounded_object_detection(
            gd_out,
            input_ids=gd_inputs.input_ids,
            threshold=box_threshold,
            text_threshold=text_threshold,
            target_sizes=[image.size[::-1]],
        )[0]
        boxes = gd_results["boxes"].detach().cpu().numpy()
        scores = gd_results["scores"].detach().cpu().numpy()
        # API rename: 'labels' -> 'text_labels' in transformers 5.x
        labels = gd_results.get("text_labels", gd_results.get("labels"))

        if len(boxes) == 0:
            continue

        # 2) SAM 2 -> masks for each box (transformers 5.x API)
        sam_inputs = sam_proc(images=image, input_boxes=[boxes.tolist()], return_tensors="pt").to(device)
        with torch.no_grad():
            sam_out = sam_model(**sam_inputs, multimask_output=False)
        masks = sam_proc.post_process_masks(
            sam_out.pred_masks.cpu(),
            sam_inputs["original_sizes"].cpu(),
        )[0].numpy()  # (N, 1, H, W) bool

        # 3) save masks + crops + manifest entries
        frame_out = out_dir / fp.stem
        frame_out.mkdir(exist_ok=True)
        for i, (box, label, score, mask) in enumerate(zip(boxes, labels, scores, masks)):
            mask_2d = mask.squeeze().astype(np.uint8) * 255
            Image.fromarray(mask_2d).save(frame_out / f"{label}_{i:02d}_mask.png")

            if save_crops:
                x1, y1, x2, y2 = [int(v) for v in box]
                crop = image.crop((x1, y1, x2, y2))
                crop.save(frame_out / f"{label}_{i:02d}_crop.jpg")

            manifest.append({
                "frame": fp.name,
                "instance_id": f"{fp.stem}/{label}_{i:02d}",
                "class": str(label),
                "score": float(score),
                "bbox": [float(v) for v in box],
                "area_px": int(mask_2d.sum() // 255),
            })

    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"Segmented {len(frame_paths)} frames -> {len(manifest)} instances. Manifest: {out_dir / 'manifest.json'}")


if __name__ == "__main__":
    app()
