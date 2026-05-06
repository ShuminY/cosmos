"""Image and video analyzers — EXIF, thumbnail, codec info."""
from __future__ import annotations
import json
import subprocess
from pathlib import Path

from PIL import Image, ExifTags


def analyze_image(path: Path) -> dict:
    img = Image.open(path)
    w, h = img.size
    fmt = img.format
    mode = img.mode
    exif_data = {}
    try:
        raw = img._getexif() or {}
        exif_data = {ExifTags.TAGS.get(k, str(k)): str(v)[:200]
                     for k, v in raw.items() if k in ExifTags.TAGS}
    except Exception:
        pass
    return {
        "format": "image",
        "summary": f"{fmt} {w}×{h} {mode}",
        "width": w,
        "height": h,
        "image_format": fmt,
        "mode": mode,
        "exif": exif_data,
    }


def analyze_video(path: Path) -> dict:
    """Use ffprobe to get duration, codec, size."""
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "error",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(path)
        ], stderr=subprocess.DEVNULL, timeout=30)
        data = json.loads(out)
    except Exception as e:
        return {
            "format": "video",
            "summary": f"ffprobe failed: {e}",
        }
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    duration_s = float(fmt.get("duration", 0) or 0)
    size_b = int(fmt.get("size", 0) or 0)
    summary_bits = [f"{duration_s:.1f}s", f"{size_b/1e6:.1f} MB"]
    if video_streams:
        v = video_streams[0]
        summary_bits.append(f"{v.get('codec_name','?')} {v.get('width','?')}×{v.get('height','?')}")
    return {
        "format": "video",
        "summary": " · ".join(summary_bits),
        "duration_s": duration_s,
        "size_bytes": size_b,
        "n_video_streams": len(video_streams),
        "n_audio_streams": len(audio_streams),
        "video": video_streams[0] if video_streams else None,
    }
