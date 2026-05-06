"""COLMAP reconstruction: SfM (sparse) + MVS (dense point cloud).

Usage:
    python src/02_reconstruct.py <frames_dir> <out_dir> [--dense]

Produces:
    out_dir/
      database.db
      sparse/0/        # cameras, images, points (text + bin)
      dense/           # PMVS-style dense reconstruction (if --dense)
        fused.ply      # merged dense point cloud
"""
from __future__ import annotations
import subprocess
from pathlib import Path
import typer

app = typer.Typer(add_completion=False)


def run(cmd: list[str]):
    print(">", " ".join(cmd))
    subprocess.run(cmd, check=True)


@app.command()
def main(
    frames: Path = typer.Argument(..., exists=True, file_okay=False),
    out_dir: Path = typer.Argument(...),
    dense: bool = typer.Option(True, help="Run dense MVS after sparse SfM"),
    matcher: str = typer.Option("sequential", help="exhaustive | sequential | vocab_tree"),
):
    out_dir.mkdir(parents=True, exist_ok=True)
    db = out_dir / "database.db"
    sparse = out_dir / "sparse"
    sparse.mkdir(exist_ok=True)

    # 1) feature extraction
    run([
        "colmap", "feature_extractor",
        "--database_path", str(db),
        "--image_path", str(frames),
        "--ImageReader.single_camera", "1",
    ])

    # 2) match
    matcher_cmd = {
        "exhaustive": "exhaustive_matcher",
        "sequential": "sequential_matcher",
        "vocab_tree": "vocab_tree_matcher",
    }[matcher]
    run([
        "colmap", matcher_cmd,
        "--database_path", str(db),
    ])

    # 3) sparse mapping
    run([
        "colmap", "mapper",
        "--database_path", str(db),
        "--image_path", str(frames),
        "--output_path", str(sparse),
    ])

    if not dense:
        return

    # 4) dense MVS — undistort then patch_match_stereo + stereo_fusion.
    # PatchMatchStereo without CUDA is unreliable on macOS (often SIGABRT);
    # we treat the dense step as best-effort so a failure doesn't lose the
    # sparse reconstruction we already have.
    dense_dir = out_dir / "dense"
    dense_dir.mkdir(exist_ok=True)
    try:
        run([
            "colmap", "image_undistorter",
            "--image_path", str(frames),
            "--input_path", str(sparse / "0"),
            "--output_path", str(dense_dir),
            "--output_type", "COLMAP",
        ])
        run([
            "colmap", "patch_match_stereo",
            "--workspace_path", str(dense_dir),
            "--PatchMatchStereo.geom_consistency", "true",
        ])
        run([
            "colmap", "stereo_fusion",
            "--workspace_path", str(dense_dir),
            "--input_type", "geometric",
            "--output_path", str(dense_dir / "fused.ply"),
        ])
        print(f"Dense point cloud: {dense_dir / 'fused.ply'}")
    except subprocess.CalledProcessError as e:
        print(f"WARN: dense MVS step failed ({e}). Sparse reconstruction is still usable at {sparse / '0'}.")


if __name__ == "__main__":
    app()
