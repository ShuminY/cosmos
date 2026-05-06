"""Generate a synthetic 'two-visit' dataset for end-to-end pipeline testing.

Renders a simple 3-surface scene (focal wall + floor + left side wall) from N
camera viewpoints using OpenCV homography projection — no GPU/3D-renderer
needed, deterministic, COLMAP-reconstructible.

Visit 1: bare drywall, no tiles.
Visit 2: focal wall is 50% painted blue, 25 ceramic tiles laid on the floor.

Each visit gets:
    data/visit_N/frames/frame_*.jpg
    data/visit_N/poses.json       (camera intrinsics + extrinsics — for grading)
    data/visit_N/ground_truth.json

Reference SKU images (cropped patches of the paint and tile textures) are
written to:
    data/references/blue_paint/*.jpg
    data/references/ceramic_tile_30cm/*.jpg

A 0.20 m × 0.20 m ArUco marker (DICT_4X4_50, id=23) is stamped on the focal
wall in BOTH visits at a known location, so cv2.aruco can recover metric scale
from the reconstructions.

Usage:
    python scripts/generate_synthetic.py [--n_views 30] [--seed 0]
"""
from __future__ import annotations
from pathlib import Path
import json
import argparse

import cv2
import numpy as np

# ============ scene constants ============
ROOM_W = 4.0   # focal wall width  (x: -2..+2)
ROOM_D = 4.0   # depth             (y: 0..4, focal wall at y=4)
ROOM_H = 2.5   # height            (z: 0..2.5)

PX_PER_M = 512  # texture resolution

# ArUco marker — physical size 0.20 m, placed at known wall-coords (in meters,
# from the wall's TOP-LEFT corner when looking at the wall from inside the room)
ARUCO_SIZE_M = 0.20
ARUCO_TL_M = (3.20, 0.30)   # (x_from_wall_left, z_from_wall_top)
ARUCO_DICT = cv2.aruco.DICT_4X4_50
ARUCO_ID = 23

# Tile geometry (laid in upper-left corner of floor when viewed from above,
# i.e., the wall-side / far-left region — first tile starts at world (-2, 4))
TILE_SIZE_M = 0.30
TILE_GROUT_M = 0.005
TILES_PER_ROW = 5

# Image / camera
IMG_W, IMG_H = 1280, 720
FOV_H_DEG = 65.0


# ============ texture generation ============
def _add_noise_features(img: np.ndarray, rng: np.random.Generator, n_blobs: int):
    """Stamp random colored blobs to give COLMAP feature points to lock onto."""
    H, W = img.shape[:2]
    for _ in range(n_blobs):
        cx = int(rng.integers(0, W))
        cy = int(rng.integers(0, H))
        r = int(rng.integers(6, 20))
        col = rng.integers(0, 255, 3, dtype=np.uint8).tolist()
        cv2.circle(img, (cx, cy), r, col, -1)
    return img


def make_wall_texture(painted_left_fraction: float, seed: int) -> np.ndarray:
    """Returns BGR uint8 wall texture sized ROOM_W × ROOM_H meters."""
    rng = np.random.default_rng(seed)
    W = int(ROOM_W * PX_PER_M)
    H = int(ROOM_H * PX_PER_M)
    img = np.full((H, W, 3), (215, 220, 225), dtype=np.uint8)  # off-white drywall
    img = np.clip(img.astype(np.int16) + rng.integers(-15, 15, img.shape, dtype=np.int16), 0, 255).astype(np.uint8)
    img = _add_noise_features(img, rng, n_blobs=120)

    # Paint left fraction blue (BGR ~ (180, 90, 60))
    if painted_left_fraction > 0:
        cut = int(W * painted_left_fraction)
        paint = np.full((H, cut, 3), (180, 90, 60), dtype=np.uint8)
        paint = np.clip(paint.astype(np.int16) + rng.integers(-12, 12, paint.shape, dtype=np.int16), 0, 255).astype(np.uint8)
        # Subtle texture features within the paint
        paint = _add_noise_features(paint, rng, n_blobs=40)
        img[:, :cut] = paint

    # Stamp ArUco marker — same in both visits, fixed position
    aruco_dict = cv2.aruco.getPredefinedDictionary(ARUCO_DICT)
    marker_px = int(ARUCO_SIZE_M * PX_PER_M)
    marker = cv2.aruco.generateImageMarker(aruco_dict, ARUCO_ID, marker_px)
    marker_bgr = cv2.cvtColor(marker, cv2.COLOR_GRAY2BGR)
    # Convert wall-coords to texture pixels: x grows left-to-right, z grows top-to-bottom in texture
    mx = int(ARUCO_TL_M[0] * PX_PER_M)
    my = int(ARUCO_TL_M[1] * PX_PER_M)
    img[my:my + marker_px, mx:mx + marker_px] = marker_bgr
    return img


def make_floor_texture(n_tiles: int, seed: int) -> np.ndarray:
    """Returns BGR uint8 floor texture sized ROOM_W × ROOM_D meters."""
    rng = np.random.default_rng(seed)
    W = int(ROOM_W * PX_PER_M)
    H = int(ROOM_D * PX_PER_M)
    # Concrete-ish gray
    img = np.full((H, W, 3), (138, 142, 148), dtype=np.uint8)
    img = np.clip(img.astype(np.int16) + rng.integers(-20, 20, img.shape, dtype=np.int16), 0, 255).astype(np.uint8)
    img = _add_noise_features(img, rng, n_blobs=200)

    # Lay tiles starting from the FAR-LEFT corner of the floor (the corner
    # against the focal wall and left wall). In texture coords:
    # focal wall is at y_world = ROOM_D, but in the floor texture we map
    # texture-y=0 to world-y=ROOM_D (so first tile is at top of texture).
    tile_px = int(TILE_SIZE_M * PX_PER_M)
    grout_px = max(2, int(TILE_GROUT_M * PX_PER_M))
    placed = 0
    for r in range(0, H // tile_px):
        for c in range(0, TILES_PER_ROW):
            if placed >= n_tiles:
                break
            y0, x0 = r * tile_px, c * tile_px
            tile_color = (240, 235, 225)  # warm white BGR
            cv2.rectangle(img, (x0, y0), (x0 + tile_px - 1, y0 + tile_px - 1), tile_color, -1)
            # Per-tile noise for COLMAP features
            t_noise = rng.integers(-10, 10, (tile_px, tile_px, 3), dtype=np.int16)
            section = img[y0:y0 + tile_px, x0:x0 + tile_px].astype(np.int16) + t_noise
            img[y0:y0 + tile_px, x0:x0 + tile_px] = np.clip(section, 0, 255).astype(np.uint8)
            # Grout outline
            cv2.rectangle(img, (x0, y0), (x0 + tile_px - 1, y0 + tile_px - 1), (90, 90, 90), grout_px)
            placed += 1
        if placed >= n_tiles:
            break
    return img


def make_side_wall_texture(seed: int) -> np.ndarray:
    """Plain drywall, same in both visits — visual reference / context."""
    rng = np.random.default_rng(seed)
    W = int(ROOM_D * PX_PER_M)   # depth maps to side-wall width
    H = int(ROOM_H * PX_PER_M)
    img = np.full((H, W, 3), (215, 220, 225), dtype=np.uint8)
    img = np.clip(img.astype(np.int16) + rng.integers(-15, 15, img.shape, dtype=np.int16), 0, 255).astype(np.uint8)
    return _add_noise_features(img, rng, n_blobs=140)


# ============ camera math ============
def look_at(eye, target, up=(0.0, 0.0, 1.0)) -> np.ndarray:
    """Return 4x4 world->camera extrinsic in OpenCV convention (x right, y down, z forward)."""
    eye = np.asarray(eye, dtype=np.float64)
    target = np.asarray(target, dtype=np.float64)
    up = np.asarray(up, dtype=np.float64)
    f = target - eye
    f /= np.linalg.norm(f)
    r = np.cross(f, up)
    r /= np.linalg.norm(r)
    u = np.cross(r, f)
    R = np.stack([r, -u, f], axis=0)  # OpenCV camera frame
    t = -R @ eye
    Rt = np.eye(4)
    Rt[:3, :3] = R
    Rt[:3, 3] = t
    return Rt


def make_intrinsics() -> np.ndarray:
    fx = (IMG_W / 2.0) / np.tan(np.deg2rad(FOV_H_DEG / 2.0))
    return np.array([[fx, 0, IMG_W / 2.0],
                     [0, fx, IMG_H / 2.0],
                     [0, 0, 1.0]])


def project(P_world: np.ndarray, K: np.ndarray, Rt: np.ndarray):
    """Project (3,) world point. Returns (2,) image coord or None if behind camera."""
    P_cam = Rt[:3, :3] @ P_world + Rt[:3, 3]
    if P_cam[2] <= 1e-3:
        return None
    p = K @ P_cam
    return p[:2] / p[2]


# ============ rendering ============
def render_view(planes: dict, K: np.ndarray, Rt: np.ndarray) -> np.ndarray:
    """Composite planes back-to-front using painter's algorithm."""
    out = np.zeros((IMG_H, IMG_W, 3), dtype=np.uint8)
    # Sort by camera-space z of centroid, descending (farthest first)
    order = []
    for name, plane in planes.items():
        c = plane["corners_world"].mean(0)
        z = (Rt[:3, :3] @ c + Rt[:3, 3])[2]
        order.append((z, name))
    order.sort(key=lambda x: -x[0])

    for _, name in order:
        plane = planes[name]
        corners_w = plane["corners_world"]   # (4, 3)  TL, TR, BR, BL in texture order
        tex = plane["texture"]
        # Project all 4 corners
        pts2d = []
        for cw in corners_w:
            p = project(cw, K, Rt)
            if p is None:
                pts2d = None
                break
            pts2d.append(p)
        if pts2d is None:
            continue
        pts2d = np.array(pts2d, dtype=np.float32)

        tH, tW = tex.shape[:2]
        tex_corners = np.array([[0, 0], [tW, 0], [tW, tH], [0, tH]], dtype=np.float32)
        H_mat, _ = cv2.findHomography(tex_corners, pts2d)
        if H_mat is None:
            continue
        warped = cv2.warpPerspective(tex, H_mat, (IMG_W, IMG_H))

        mask = np.zeros((IMG_H, IMG_W), dtype=np.uint8)
        cv2.fillConvexPoly(mask, pts2d.astype(np.int32), 255)
        out[mask > 0] = warped[mask > 0]
    return out


# ============ scene assembly ============
def build_planes(wall_tex, floor_tex, side_tex) -> dict:
    """Define each plane's world-space corner positions in texture order (TL, TR, BR, BL)."""
    return {
        # Focal wall — at y = ROOM_D, viewed from inside (camera looks +y)
        # Texture orientation: TL = top-left as seen from inside (x = -W/2, z = H)
        "focal_wall": {
            "corners_world": np.array([
                [-ROOM_W / 2, ROOM_D, ROOM_H],   # TL
                [ ROOM_W / 2, ROOM_D, ROOM_H],   # TR
                [ ROOM_W / 2, ROOM_D, 0.0    ],  # BR
                [-ROOM_W / 2, ROOM_D, 0.0    ],  # BL
            ], dtype=np.float64),
            "texture": wall_tex,
        },
        # Floor — at z = 0; texture TL maps to far-left corner (against focal+left walls)
        "floor": {
            "corners_world": np.array([
                [-ROOM_W / 2, ROOM_D, 0.0],   # TL = far-left (texture top is far-y)
                [ ROOM_W / 2, ROOM_D, 0.0],   # TR = far-right
                [ ROOM_W / 2, 0.0,    0.0],   # BR = near-right
                [-ROOM_W / 2, 0.0,    0.0],   # BL = near-left
            ], dtype=np.float64),
            "texture": floor_tex,
        },
        # Left side wall — at x = -ROOM_W/2
        "left_wall": {
            "corners_world": np.array([
                [-ROOM_W / 2, 0.0,    ROOM_H],  # TL (near-top)
                [-ROOM_W / 2, ROOM_D, ROOM_H],  # TR (far-top)
                [-ROOM_W / 2, ROOM_D, 0.0   ],  # BR (far-bottom)
                [-ROOM_W / 2, 0.0,    0.0   ],  # BL (near-bottom)
            ], dtype=np.float64),
            "texture": side_tex,
        },
    }


def camera_path(n_views: int) -> list[np.ndarray]:
    """Sweep camera positions OUTSIDE the room entrance (negative y), looking
    toward the focal wall and angled down so floor + side wall enter the FOV.
    Keeping the camera outside the room means all floor / side-wall corners
    are in front of the camera (no near-plane clipping needed)."""
    poses = []
    for i in range(n_views):
        t = i / max(1, n_views - 1)
        # Lateral sweep, with mild forward arc and small height variation
        eye_x = -1.40 + 2.80 * t
        eye_y = -1.50 + 0.40 * np.sin(t * np.pi)
        eye_z = 1.55 + 0.10 * np.sin(t * 2 * np.pi)
        # Target lower so we tilt down — floor (and tiles) fill the lower half,
        # focal wall fills the upper half, side wall flickers in/out for parallax
        target = np.array([eye_x * 0.15, ROOM_D, 0.80])
        Rt = look_at([eye_x, eye_y, eye_z], target)
        poses.append(Rt)
    return poses


# ============ reference SKU crops ============
def write_reference_crops(wall_tex_v2: np.ndarray, floor_tex_v2: np.ndarray, ref_dir: Path):
    """Save closeup crops of the paint and tile as user-supplied 'reference SKUs'."""
    paint_dir = ref_dir / "blue_paint"
    tile_dir = ref_dir / "ceramic_tile_30cm"
    paint_dir.mkdir(parents=True, exist_ok=True)
    tile_dir.mkdir(parents=True, exist_ok=True)
    # Paint crops from the painted (left) half of the wall texture
    H, W = wall_tex_v2.shape[:2]
    paint_region = wall_tex_v2[:, :W // 2]
    rng = np.random.default_rng(7)
    for i in range(4):
        y = int(rng.integers(50, paint_region.shape[0] - 350))
        x = int(rng.integers(50, paint_region.shape[1] - 350))
        crop = paint_region[y:y + 300, x:x + 300]
        cv2.imwrite(str(paint_dir / f"swatch_{i:02d}.jpg"), crop)
    # Tile crops — sample from a known-tile region (top-left)
    tile_px = int(TILE_SIZE_M * PX_PER_M)
    for i in range(4):
        r = int(rng.integers(0, 2))
        c = int(rng.integers(0, TILES_PER_ROW - 1))
        y0 = r * tile_px + 10
        x0 = c * tile_px + 10
        crop = floor_tex_v2[y0:y0 + tile_px - 20, x0:x0 + tile_px - 20]
        cv2.imwrite(str(tile_dir / f"tile_{i:02d}.jpg"), crop)


# ============ main ============
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n_views", type=int, default=30)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--root", type=Path, default=Path("/Users/bytedance/cosmos"))
    args = parser.parse_args()

    K = make_intrinsics()
    poses = camera_path(args.n_views)

    # Visit configurations
    visits = {
        1: {"painted_fraction": 0.0, "n_tiles": 0},
        2: {"painted_fraction": 0.50, "n_tiles": 25},
    }

    # Pre-build textures so we can save reference crops from visit 2 textures
    textures = {}
    for v, cfg in visits.items():
        textures[v] = {
            "wall":  make_wall_texture(cfg["painted_fraction"], seed=args.seed + 100 + v),
            "floor": make_floor_texture(cfg["n_tiles"],         seed=args.seed + 200 + v),
            "side":  make_side_wall_texture(                    seed=args.seed + 300 + v),
        }

    # Write reference SKU crops from visit 2 textures
    write_reference_crops(textures[2]["wall"], textures[2]["floor"], args.root / "data" / "references")

    for v, cfg in visits.items():
        out_frames = args.root / "data" / f"visit_{v}" / "frames"
        out_frames.mkdir(parents=True, exist_ok=True)
        planes = build_planes(textures[v]["wall"], textures[v]["floor"], textures[v]["side"])

        pose_records = []
        for i, Rt in enumerate(poses):
            img = render_view(planes, K, Rt)
            cv2.imwrite(str(out_frames / f"frame_{i:05d}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 92])
            pose_records.append({
                "frame": f"frame_{i:05d}.jpg",
                "Rt_world_from_camera_inverse": Rt.tolist(),  # i.e. world->camera
            })

        # Per-visit metadata
        wall_painted_area_m2 = cfg["painted_fraction"] * ROOM_W * ROOM_H
        gt = {
            "visit": v,
            "wall": {
                "total_area_m2": ROOM_W * ROOM_H,
                "painted_fraction": cfg["painted_fraction"],
                "painted_area_m2": wall_painted_area_m2,
                "paint_color_name": "blue_paint" if cfg["painted_fraction"] > 0 else None,
            },
            "floor": {
                "total_area_m2": ROOM_W * ROOM_D,
                "tile_count": cfg["n_tiles"],
                "tile_size_m": TILE_SIZE_M,
                "tiled_area_m2": cfg["n_tiles"] * TILE_SIZE_M ** 2,
                "tile_sku": "ceramic_tile_30cm" if cfg["n_tiles"] > 0 else None,
            },
            "aruco": {
                "dict": "DICT_4X4_50",
                "id": ARUCO_ID,
                "size_m": ARUCO_SIZE_M,
            },
            "room_dims_m": {"W": ROOM_W, "D": ROOM_D, "H": ROOM_H},
        }
        (args.root / "data" / f"visit_{v}" / "ground_truth.json").write_text(json.dumps(gt, indent=2))
        (args.root / "data" / f"visit_{v}" / "poses.json").write_text(json.dumps({
            "K": K.tolist(),
            "img_size": [IMG_W, IMG_H],
            "poses": pose_records,
        }, indent=2))
        print(f"visit_{v}: wrote {len(poses)} frames + ground_truth.json + poses.json")

    # Top-level diff (what the system should ideally find)
    diff = {
        "expected_changes": [
            {
                "element": "focal_wall",
                "type": "painted",
                "sku": "blue_paint",
                "delta_area_m2": visits[2]["painted_fraction"] * ROOM_W * ROOM_H - visits[1]["painted_fraction"] * ROOM_W * ROOM_H,
                "delta_fraction": visits[2]["painted_fraction"] - visits[1]["painted_fraction"],
            },
            {
                "element": "floor",
                "type": "tiles_laid",
                "sku": "ceramic_tile_30cm",
                "delta_count": visits[2]["n_tiles"] - visits[1]["n_tiles"],
                "delta_area_m2": (visits[2]["n_tiles"] - visits[1]["n_tiles"]) * TILE_SIZE_M ** 2,
            },
        ],
    }
    (args.root / "data" / "expected_diff.json").write_text(json.dumps(diff, indent=2))
    print(f"\nExpected diff written: {args.root / 'data' / 'expected_diff.json'}")
    print("Synthetic dataset ready.")


if __name__ == "__main__":
    main()
