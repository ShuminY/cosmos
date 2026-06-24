#!/usr/bin/env python3
"""Aholo 3DGS 重建任务脚本 - 使用Aholo API进行图片3D建模.

Usage:
    python scripts/run_aholo3d.py --job-id <id> --doc-id <id> --project-id <id> \
        --image-path <path> --output-dir <path> --status-path <path>
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 优先使用技能中的客户端，否则使用项目自定义客户端
try:
    sys.path.insert(0, str(ROOT / "skills" / "aholo-3dgs-reconstruction"))
    from aholo_reconstruct import AholoClient, SITE_CONFIG
    USE_SKILL_CLIENT = True
except ImportError:
    from src.aholo3d_client import Aholo3DClient as AholoClient
    SITE_CONFIG = {"base_url": "https://api.aholo3d.cn"}
    USE_SKILL_CLIENT = False


def update_status(status_path: Path, **kwargs):
    """更新状态文件."""
    if status_path.exists():
        cur = json.loads(status_path.read_text())
    else:
        cur = {}
    cur.update(kwargs)
    cur["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    status_path.write_text(json.dumps(cur, indent=2))


def mark(status_path: Path, stage: str, status: str, msg: str = ""):
    """标记阶段状态."""
    if not status_path.exists():
        return
    cur = json.loads(status_path.read_text())
    for s in cur.get("stages", []):
        if s["name"] == stage:
            s["status"] = status
            if msg:
                s["message"] = msg
    update_status(status_path, stages=cur["stages"])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--job-id", required=True)
    p.add_argument("--doc-id", type=int, required=True)
    p.add_argument("--project-id", type=int, required=True)
    p.add_argument("--image-path", required=True, type=Path)
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--status-path", required=True, type=Path)
    p.add_argument("--prompt", type=str, default=None,
                   help="3D重建提示词（可选）")
    args = p.parse_args()

    # 确保输出目录存在
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # 检测并转换 WebP 图片为真实 PNG（OUS 上传不接受 WebP 格式文件）
    img_path = str(args.image_path)
    try:
        import struct
        with open(img_path, 'rb') as fh:
            header = fh.read(12)
        # WebP 文件头: RIFF....WEBP
        if header[0:4] == b'RIFF' and header[8:12] == b'WEBP':
            print(f"[{args.job_id}] Detected WebP format, converting to PNG...")
            from PIL import Image
            webp_img = Image.open(img_path)
            converted_path = args.output_dir / f"{args.image_path.stem}_converted.png"
            webp_img.save(str(converted_path), 'PNG')
            img_path = str(converted_path)
            print(f"[{args.job_id}] Converted to: {converted_path}")
    except Exception:
        pass  # 非图片或转换失败则使用原文件

    # 获取 API Key
    import os
    api_key = os.environ.get("AHOLO_API_KEY", "").strip()
    if not api_key:
        # 尝试从 .env 文件读取（兼容 AHOLO_API_KEY 和 AHOLO3D_API_KEY）
        env_file = ROOT / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line.startswith("AHOLO_API_KEY=") or line.startswith("AHOLO3D_API_KEY="):
                    api_key = line.split("=", 1)[1].strip().strip('"\'')
                    break

    if not api_key:
        update_status(
            args.status_path,
            overall_status="failed",
            error="AHOLO_API_KEY not configured. Please set it in .env file or environment variable.",
            finished_at=time.strftime("%Y-%m-%d %H:%M:%S")
        )
        print("Error: AHOLO_API_KEY not configured", file=sys.stderr)
        return 1

    # 初始化客户端
    client = AholoClient(api_key=api_key)

    try:
        # 阶段1: 上传图片
        mark(args.status_path, "upload", "running")
        print(f"[{args.job_id}] Uploading image: {args.image_path}")

        upload_result = client.upload_file(img_path)
        if not upload_result.get("success"):
            mark(args.status_path, "upload", "failed", upload_result.get("error"))
            update_status(args.status_path, overall_status="failed",
                         error=f"Upload failed: {upload_result.get('error')}",
                         finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
            print(f"[{args.job_id}] Upload failed: {upload_result.get('error')}", file=sys.stderr)
            return 1
        image_url = upload_result.get("url")
        mark(args.status_path, "upload", "done", f"url={image_url[:50]}...")
        update_status(args.status_path, image_url=image_url)
        print(f"[{args.job_id}] Upload complete, url: {image_url[:50]}...")

        # 阶段2: 创建重建任务
        mark(args.status_path, "create_task", "running")
        print(f"[{args.job_id}] Creating reconstruction task...")

        create_result = client.create_generation(
            project_name=f"Cosmos_{args.doc_id}_{args.image_path.stem}",
            prompt=args.prompt,
            resources=[{"url": image_url, "type": "image"}],
        )

        if not create_result.get("success"):
            mark(args.status_path, "create_task", "failed", create_result.get("error"))
            update_status(
                args.status_path,
                overall_status="failed",
                error=f"Create task failed: {create_result.get('error')}",
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S")
            )
            print(f"[{args.job_id}] Create task failed: {create_result.get('error')}", file=sys.stderr)
            return 1

        world_id = create_result.get("worldId")
        mark(args.status_path, "create_task", "done", f"world_id={world_id}")
        update_status(args.status_path, world_id=world_id)
        print(f"[{args.job_id}] World created, world_id: {world_id}")

        # 阶段3: 等待处理完成
        mark(args.status_path, "processing", "running")
        print(f"[{args.job_id}] Waiting for processing...")

        if USE_SKILL_CLIENT:
            poll_result = client.poll_project_until_terminal(
                world_id=world_id, interval_seconds=30, timeout_seconds=3600,
            )
        else:
            final_task = client.wait_for_completion(
                world_id, poll_interval=30, max_wait=3600,
            )
            if final_task.status == "completed":
                assets = client.get_world_assets(world_id)
                ply_path = None
                spz_path = None
                for asset in assets.get("assets", []):
                    if asset.get("type") == "ply":
                        ply_path = asset.get("url")
                    elif asset.get("type") == "spz":
                        spz_path = asset.get("url")
                poll_result = {
                    "success": True,
                    "task": {"status": "SUCCEEDED", "isSuccess": True, "isTerminal": True},
                    "result": {"plyPath": ply_path, "spzPath": spz_path},
                }
            else:
                poll_result = {
                    "success": False,
                    "error": final_task.error or "Processing failed",
                }

        if not poll_result.get("success"):
            mark(args.status_path, "processing", "failed", poll_result.get("error"))
            update_status(
                args.status_path,
                overall_status="failed",
                error=f"Processing failed: {poll_result.get('error')}",
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S")
            )
            print(f"[{args.job_id}] Processing failed: {poll_result.get('error')}", file=sys.stderr)
            return 1

        task = poll_result.get("task", {})
        is_success = task.get("isSuccess")
        is_terminal = task.get("isTerminal")

        if not is_success:
            mark(args.status_path, "processing", "failed", f"status={task.get('status')}")
            update_status(
                args.status_path,
                overall_status="failed",
                error=f"Task failed with status: {task.get('status')}",
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S")
            )
            print(f"[{args.job_id}] Task failed with status: {task.get('status')}", file=sys.stderr)
            return 1

        mark(args.status_path, "processing", "done", f"status={task.get('status')}")
        print(f"[{args.job_id}] Processing complete")

        # 阶段4: 下载生成的资产
        mark(args.status_path, "download", "running")
        print(f"[{args.job_id}] Downloading assets...")

        result = poll_result.get("result", {})
        output_files = []

        # 下载 PLY 文件
        ply_url = result.get("plyPath")
        if ply_url:
            ply_path = args.output_dir / "model.ply"
            try:
                import requests
                resp = requests.get(ply_url, stream=True, timeout=300)
                resp.raise_for_status()
                with open(ply_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                output_files.append(str(ply_path))
                print(f"[{args.job_id}] Downloaded PLY: {ply_path}")
            except Exception as e:
                print(f"[{args.job_id}] Failed to download PLY: {e}")

        # 下载 SPZ 文件
        spz_url = result.get("spzPath")
        if spz_url:
            spz_path = args.output_dir / "model.spz"
            try:
                import requests
                resp = requests.get(spz_url, stream=True, timeout=300)
                resp.raise_for_status()
                with open(spz_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                output_files.append(str(spz_path))
                print(f"[{args.job_id}] Downloaded SPZ: {spz_path}")
            except Exception as e:
                print(f"[{args.job_id}] Failed to download SPZ: {e}")

        # 下载 SOG 文件
        sog_url = result.get("sogPath")
        if sog_url:
            sog_path = args.output_dir / "model.sog"
            try:
                import requests
                resp = requests.get(sog_url, stream=True, timeout=300)
                resp.raise_for_status()
                with open(sog_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                output_files.append(str(sog_path))
                print(f"[{args.job_id}] Downloaded SOG: {sog_path}")
            except Exception as e:
                print(f"[{args.job_id}] Failed to download SOG: {e}")

        mark(args.status_path, "download", "done",
             f"{len(output_files)} files" if output_files else "view only (no download links)")

        # 生成 viewer URL
        viewer_url = f"https://studio.aholo3d.cn/3dgs-model/{world_id}"

        # 最终状态更新
        update_status(
            args.status_path,
            overall_status="done",
            finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            world_id=world_id,
            image_url=image_url,
            viewer_url=viewer_url,
            result=result,
            output_files=output_files,
        )

        print(f"[{args.job_id}] All done! Output files: {output_files}")
        return 0

    except Exception as e:
        import traceback
        error_msg = f"{type(e).__name__}: {str(e)}"
        traceback_str = traceback.format_exc()

        update_status(
            args.status_path,
            overall_status="failed",
            error=error_msg,
            traceback=traceback_str,
            finished_at=time.strftime("%Y-%m-%d %H:%M:%S")
        )
        print(f"[{args.job_id}] Error: {error_msg}", file=sys.stderr)
        print(traceback_str, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
