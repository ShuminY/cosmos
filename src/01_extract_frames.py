"""Extract frames from a video at a target FPS for COLMAP / segmentation.

Usage:
    python src/01_extract_frames.py <video_path> <out_dir> [--fps 2]
"""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path
import typer

app = typer.Typer(add_completion=False)


@app.command()
def main(
    video: Path = typer.Argument(..., exists=True, file_okay=True, dir_okay=False),
    out_dir: Path = typer.Argument(...),
    fps: float = typer.Option(2.0, help="Frames per second to extract"),
    quality: int = typer.Option(2, help="ffmpeg JPEG quality (1=best, 31=worst)"),
):
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-i", str(video),
        "-vf", f"fps={fps}",
        "-q:v", str(quality),
        str(out_dir / "frame_%05d.jpg"),
    ]
    print(" ".join(cmd))
    subprocess.run(cmd, check=True)
    n = len(list(out_dir.glob("frame_*.jpg")))
    print(f"Extracted {n} frames -> {out_dir}")


if __name__ == "__main__":
    app()
