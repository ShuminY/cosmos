"""Shared config — paths, device selection, model IDs."""
from __future__ import annotations
from pathlib import Path
import torch

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUTPUTS = ROOT / "outputs"
MODELS = ROOT / "models"


def get_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


# Hugging Face model IDs — pinned for reproducibility.
# Defaulting to *tiny* variants for the Mac/MPS POC. Bump to large for cloud GPU.
SAM2_HF = "facebook/sam2.1-hiera-tiny"           # ~150 MB
GROUNDING_DINO_HF = "IDEA-Research/grounding-dino-tiny"  # ~700 MB
DINOV2_HF = "facebook/dinov2-small"              # ~85 MB

# Default text prompts for Grounded-SAM-2 (open-vocab)
ELEMENT_PROMPTS = [
    "wall",
    "floor",
    "ceiling",
    "door",
    "window",
    "ceramic tile",
    "tile",
]
