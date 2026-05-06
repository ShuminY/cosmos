"""Match segmented element crops against user-supplied reference images using
DINOv2 embeddings + cosine similarity.

Usage:
    python src/05_match_references.py <segments_dir> <references_dir> <out_path>

Segments dir layout (from 04_segment.py):
    <frame_stem>/<class>_<idx>_crop.jpg

References dir layout:
    <references_dir>/<sku_name>/{*.jpg,*.png}   — one folder per SKU
"""
from __future__ import annotations
from pathlib import Path
import json
import sys

import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image
from tqdm import tqdm
import typer
from transformers import AutoImageProcessor, AutoModel

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import DINOV2_HF, get_device

app = typer.Typer(add_completion=False)


def embed_images(paths: list[Path], processor, model, device: str) -> np.ndarray:
    """Return (N, D) L2-normalized CLS embeddings."""
    embs = []
    for p in paths:
        img = Image.open(p).convert("RGB")
        inputs = processor(images=img, return_tensors="pt").to(device)
        with torch.no_grad():
            out = model(**inputs)
        # DINOv2 outputs last_hidden_state; CLS token is index 0
        cls = out.last_hidden_state[:, 0]
        cls = F.normalize(cls, dim=-1)
        embs.append(cls.cpu().numpy()[0])
    return np.stack(embs) if embs else np.zeros((0, model.config.hidden_size))


@app.command()
def main(
    segments: Path = typer.Argument(..., exists=True, file_okay=False),
    references: Path = typer.Argument(..., exists=True, file_okay=False),
    out_path: Path = typer.Argument(...),
    threshold: float = typer.Option(0.60, help="Cosine similarity cutoff for a match"),
):
    device = get_device()
    print(f"device={device}  dinov2={DINOV2_HF}")
    processor = AutoImageProcessor.from_pretrained(DINOV2_HF)
    model = AutoModel.from_pretrained(DINOV2_HF).to(device).eval()

    # Build reference index — mean-pooled embedding per SKU folder.
    # Recurse so both <ref>/<sku>/*.jpg and <ref>/<sku>/images/*.jpg work.
    sku_dirs = [d for d in references.iterdir() if d.is_dir()]
    if not sku_dirs:
        raise typer.BadParameter(f"No SKU subdirs in {references}")
    sku_embs: dict[str, np.ndarray] = {}
    for skud in tqdm(sku_dirs, desc="ref index"):
        ref_paths = sorted([p for p in skud.rglob("*")
                            if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png"}])
        if not ref_paths:
            continue
        embs = embed_images(ref_paths, processor, model, device)
        sku_embs[skud.name] = embs.mean(0) / (np.linalg.norm(embs.mean(0)) + 1e-8)
    sku_names = list(sku_embs.keys())
    sku_matrix = np.stack([sku_embs[k] for k in sku_names])  # (S, D)

    # Embed each segment crop
    crops = sorted(segments.glob("*/*_crop.jpg"))
    print(f"Matching {len(crops)} crops against {len(sku_names)} SKUs...")
    matches: dict[str, dict] = {}
    for cp in tqdm(crops, desc="match"):
        emb = embed_images([cp], processor, model, device)[0]
        sims = sku_matrix @ emb
        best = int(sims.argmax())
        score = float(sims[best])
        matches[str(cp.relative_to(segments))] = {
            "sku": sku_names[best] if score >= threshold else None,
            "score": score,
            "all_scores": {n: float(s) for n, s in zip(sku_names, sims)},
        }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(matches, indent=2))
    print(f"Wrote {len(matches)} matches -> {out_path}")


if __name__ == "__main__":
    app()
