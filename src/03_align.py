"""Register visit_2 point cloud to visit_1 via global RANSAC + ICP refinement.

Usage:
    python src/03_align.py <visit_1_dir> <visit_2_dir>

Reads:   <visit_N>/dense/fused.ply  (preferred), falling back to
         <visit_N>/sparse/0/points3D.ply  if dense is missing.
Writes:  <visit_2_dir>/aligned_to_visit_1.ply  +  transform.npy (4x4)
"""
from __future__ import annotations
from pathlib import Path
import numpy as np
import open3d as o3d
import typer

app = typer.Typer(add_completion=False)


def preprocess(pcd: o3d.geometry.PointCloud, voxel: float):
    down = pcd.voxel_down_sample(voxel)
    down.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=voxel * 2, max_nn=30))
    fpfh = o3d.pipelines.registration.compute_fpfh_feature(
        down, o3d.geometry.KDTreeSearchParamHybrid(radius=voxel * 5, max_nn=100)
    )
    return down, fpfh


def load_visit_pointcloud(visit_dir: Path) -> o3d.geometry.PointCloud:
    """Prefer dense/fused.ply. Otherwise pick the LARGEST sparse sub-model PLY,
    since COLMAP often fragments and the canonical sub-model isn't always 0."""
    dense = visit_dir / "dense" / "fused.ply"
    if dense.exists():
        return o3d.io.read_point_cloud(str(dense))
    sparse_dir = visit_dir / "sparse"
    candidates = list(sparse_dir.glob("*/points3D.ply")) if sparse_dir.exists() else []
    if not candidates:
        raise FileNotFoundError(f"No dense PLY and no sparse/*/points3D.ply in {visit_dir}")
    # Pick the largest by point count
    best = max(candidates, key=lambda p: len(o3d.io.read_point_cloud(str(p)).points))
    print(f"Using {best} (selected as largest of {len(candidates)} sub-models)")
    return o3d.io.read_point_cloud(str(best))


@app.command()
def main(
    visit_1: Path = typer.Argument(..., exists=True, file_okay=False),
    visit_2: Path = typer.Argument(..., exists=True, file_okay=False),
    voxel: float = typer.Option(0.05, help="Voxel size in meters for downsampling"),
):
    src = load_visit_pointcloud(visit_2)
    tgt = load_visit_pointcloud(visit_1)
    print(f"src={len(src.points)} tgt={len(tgt.points)}")

    src_d, src_f = preprocess(src, voxel)
    tgt_d, tgt_f = preprocess(tgt, voxel)

    # Global registration via RANSAC on FPFH
    distance = voxel * 1.5
    result = o3d.pipelines.registration.registration_ransac_based_on_feature_matching(
        src_d, tgt_d, src_f, tgt_f, mutual_filter=True,
        max_correspondence_distance=distance,
        estimation_method=o3d.pipelines.registration.TransformationEstimationPointToPoint(False),
        ransac_n=4,
        criteria=o3d.pipelines.registration.RANSACConvergenceCriteria(100000, 0.999),
    )
    print("Global RANSAC fitness:", result.fitness, "inlier_rmse:", result.inlier_rmse)

    # Refine with point-to-plane ICP. Normals required on the full clouds
    # (we only computed them on the downsampled copies above).
    src.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=voxel * 2, max_nn=30))
    tgt.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=voxel * 2, max_nn=30))
    result_icp = o3d.pipelines.registration.registration_icp(
        src, tgt, voxel * 0.4, result.transformation,
        o3d.pipelines.registration.TransformationEstimationPointToPlane(),
    )
    print("ICP fitness:", result_icp.fitness, "inlier_rmse:", result_icp.inlier_rmse)

    src.transform(result_icp.transformation)
    out_ply = visit_2 / "aligned_to_visit_1.ply"
    o3d.io.write_point_cloud(str(out_ply), src)
    np.save(visit_2 / "transform.npy", result_icp.transformation)
    print(f"Wrote {out_ply}")


if __name__ == "__main__":
    app()
