"""Aholo 3DGS API Client - 基于官方 Skill 实现.

API文档: https://labs.aholo3d.cn/api-docs/quickstart

流程:
1) 获取上传凭证：GET /world/v1/asset/token
2) 本地文件上传到 OUS globalDomain
3) 创建重建任务：POST /world/v1/reconstructions
4) 查询/轮询：GET /world/v1/{worldId}
"""
from __future__ import annotations
import hashlib
import json
import mimetypes
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable

import requests
import urllib3


# API 配置
SITE_CONFIG = {
    "base_url": "https://api.aholo3d.cn",
    "path_prefix": "",
    "viewer_url_template": "https://studio.aholo3d.cn/3dgs-model/{world_id}",
    "api_keys_url": "https://labs.aholo3d.cn/api-keys",
}


def _world_api_paths(path_prefix: str) -> Dict[str, str]:
    return {
        "upload_token": f"{path_prefix}/world/v1/asset/token",
        "reconstructions": f"{path_prefix}/world/v1/reconstructions",
        "generations": f"{path_prefix}/world/v1/generations",
        "world_detail": f"{path_prefix}/world/v1/{{worldId}}",
    }


_CFG = SITE_CONFIG
_PATHS = _world_api_paths(_CFG["path_prefix"])
PATH_UPLOAD_TOKEN = _PATHS["upload_token"]
PATH_RECONSTRUCTIONS = _PATHS["reconstructions"]
PATH_GENERATIONS = _PATHS["generations"]
PATH_WORLD_DETAIL = _PATHS["world_detail"]

HEADER_X_SOURCE = "x-source"
X_SOURCE_VALUE_SKILLS = "skills"

WORLD_TERMINAL_STATUS = {"SUCCEEDED", "FAILED", "CANCELED", "TIMEOUT", "REJECTED"}
WORLD_STATUS_DESC = {
    "PENDING": "排队中",
    "WAITING": "等待执行",
    "RUNNING": "执行中",
    "SUCCEEDED": "成功",
    "FAILED": "失败",
    "CANCELED": "已取消",
    "TIMEOUT": "超时",
    "REJECTED": "被拒绝",
    "PREPROCESSING": "预处理中",
}


def _is_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


def _auth_hint(error_msg: str, code: Optional[str] = None) -> str:
    text = f"{error_msg} {code or ''}".lower()
    if any(x in text for x in ["auth", "authorization", "401", "403", "appkey", "api key", "apikey", "鉴权", "认证"]):
        return "请检查 API Key 是否正确。"
    return ""


def _format_hint(error_msg: str) -> str:
    msg = error_msg.lower()
    if "格式" in error_msg or "format" in msg or "h.264" in msg or "codec" in msg:
        return "请确保视频为标准 mp4 且编码为 H.264。"
    return ""


class Aholo3DClient:
    """Aholo 3DGS API 客户端."""

    BASE_URL = SITE_CONFIG["base_url"]

    def __init__(self, api_key: Optional[str] = None):
        """初始化客户端.

        Args:
            api_key: API密钥，如果不提供则从环境变量或配置文件读取
        """
        self.api_key = api_key or self._get_api_key()

        # SSL 验证设置
        skip_verify = str(os.environ.get("AHOLO_INSECURE_SKIP_VERIFY", "")).strip().lower()
        force_verify = str(os.environ.get("AHOLO_FORCE_SSL_VERIFY", "")).strip().lower()
        if force_verify in {"1", "true", "yes", "on"}:
            self.verify_ssl = True
        elif skip_verify in {"0", "false", "no", "off"}:
            self.verify_ssl = True
        else:
            self.verify_ssl = False  # 默认跳过证书验证

        if not self.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        self.ous_token: Optional[str] = None
        self.global_domain: Optional[str] = None
        self.block_size: int = 1024 * 1024

    def _get_api_key(self) -> str:
        """获取 API Key（环境变量 > 配置文件）."""
        # 1. 环境变量
        api_key = os.environ.get("AHOLO_API_KEY", "").strip()
        if api_key:
            return api_key

        # 2. 配置文件
        from src.config_manager import get_config
        api_key = get_config("AHOLO_API_KEY", "").strip()
        if api_key:
            return api_key

        raise ValueError(
            "Aholo 3D API Key 未配置。\n"
            "请设置以下任意一种方式:\n"
            "1. 环境变量: export AHOLO_API_KEY='your_key'\n"
            "2. .env 文件: echo 'AHOLO_API_KEY=your_key' > .env\n"
            "3. settings.yaml: 设置 aholo3d_api_key: your_key\n"
            "\n获取 API Key: https://labs.aholo3d.cn/api-keys"
        )

    @staticmethod
    def _is_open_api_error(payload: Any) -> bool:
        return isinstance(payload, dict) and "status" in payload and "message" in payload

    @staticmethod
    def _open_api_error_message(payload: Dict[str, Any], default: str = "Unknown error") -> str:
        msg = str(payload.get("message") or default)
        details = payload.get("details") or {}
        meta = details.get("metaData") or {}
        biz_code = meta.get("bizCode")
        if biz_code:
            msg = f"{msg} (bizCode={biz_code})"
        return msg

    @staticmethod
    def _open_api_biz_code(payload: Dict[str, Any]) -> Optional[str]:
        details = payload.get("details") or {}
        meta = details.get("metaData") or {}
        biz = meta.get("bizCode")
        return str(biz) if biz is not None else None

    def _parse_open_api_json(self, resp: requests.Response) -> Any:
        """解析开放平台 JSON 响应."""
        text = (resp.text or "").strip().lstrip("﻿")
        if not text:
            raise ValueError("响应体为空")
        try:
            decoder = json.JSONDecoder()
            obj, _ = decoder.raw_decode(text)
            return obj
        except json.JSONDecodeError:
            # 兼容裸 worldId 文本
            if re.fullmatch(r"[A-Za-z0-9_-]{4,200}", text):
                return text
            raise ValueError("响应解析失败: 非合法 JSON") from None

    @staticmethod
    def _world_id_from_create_payload(payload: Any) -> Optional[str]:
        """从创建接口响应中取出 worldId."""
        if isinstance(payload, str):
            s = payload.strip()
            return s if s else None
        if isinstance(payload, dict):
            for k in ("worldId", "data", "id"):
                v = payload.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        return None

    def _check_open_api_response(self, resp: requests.Response, payload: Any) -> Optional[Dict[str, Any]]:
        if resp.status_code < 400 and not self._is_open_api_error(payload):
            return None
        if not isinstance(payload, dict):
            payload = {"message": resp.text or f"HTTP {resp.status_code}"}
        msg = self._open_api_error_message(payload)
        biz_code = self._open_api_biz_code(payload)
        hint = _auth_hint(msg, biz_code or str(payload.get("code")))
        return {
            "success": False,
            "error": msg + (f"\n修复建议：{hint}" if hint else ""),
            "code": payload.get("code", resp.status_code),
        }

    def _auth_headers(self) -> Dict[str, str]:
        return {"Authorization": self.api_key, "Content-Type": "application/json"}

    def _create_task_headers(self) -> Dict[str, str]:
        """重建/生成创建接口的请求头."""
        headers = self._auth_headers()
        headers[HEADER_X_SOURCE] = X_SOURCE_VALUE_SKILLS
        return headers

    def _ous_headers(self) -> Dict[str, str]:
        if not self.ous_token:
            return {}
        return {"ous-token-v2": self.ous_token}

    @staticmethod
    def _ok(result: Dict[str, Any]) -> bool:
        """OUS V2 接口使用 c/m/d 封装."""
        return str(result.get("c")) == "0"

    @staticmethod
    def _api_error(result: Dict[str, Any], default: str = "Unknown error") -> str:
        return str(result.get("m") or default)

    # ============ 上传相关 ============

    def get_upload_token(self) -> Dict[str, Any]:
        """获取上传凭证."""
        url = f"{self.BASE_URL}{PATH_UPLOAD_TOKEN}"
        try:
            resp = requests.get(url, headers=self._auth_headers(), timeout=30, verify=self.verify_ssl)
            payload = self._parse_open_api_json(resp)
            err = self._check_open_api_response(resp, payload)
            if err:
                return err

            data = payload if isinstance(payload, dict) else {}
            self.ous_token = data.get("ousToken")
            self.global_domain = data.get("globalDomain")
            self.block_size = int(data.get("blockSize") or self.block_size)

            if not self.ous_token or not self.global_domain:
                return {"success": False, "error": "上传凭证缺失 ousToken 或 globalDomain。"}

            return {
                "success": True,
                "ousToken": self.ous_token,
                "globalDomain": self.global_domain,
                "blockSize": self.block_size,
            }
        except requests.exceptions.RequestException as e:
            return {"success": False, "error": f"请求失败: {e}"}
        except (TypeError, ValueError) as e:
            return {"success": False, "error": str(e)}

    @staticmethod
    def _calculate_md5(file_path: str) -> str:
        h = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def _poll_upload_until_ready(self, timeout_seconds: int = 120, interval_seconds: float = 0.5) -> Dict[str, Any]:
        """轮询上传状态."""
        if not self.global_domain or not self.ous_token:
            return {"success": False, "error": "缺少上传凭证。"}

        url = f"{self.global_domain}/ous/api/v2/upload/status"
        deadline = time.time() + timeout_seconds

        while time.time() < deadline:
            try:
                resp = requests.get(url, headers=self._ous_headers(), timeout=30, verify=self.verify_ssl)
                resp.raise_for_status()
                result = resp.json()
            except requests.exceptions.RequestException as e:
                return {"success": False, "error": f"查询上传状态失败: {e}"}
            except json.JSONDecodeError as e:
                return {"success": False, "error": f"上传状态 JSON 解析失败: {e}"}

            if not self._ok(result):
                return {"success": False, "error": f"查询上传状态失败: {self._api_error(result)}"}

            data = result.get("d") or {}
            status = data.get("status")
            if status == 5:
                return {"success": True, "url": data.get("url"), "uploadStatus": status}
            if status in (6, 8):
                return {
                    "success": False,
                    "error": f"上传失败，status={status}",
                }
            time.sleep(max(0.2, interval_seconds))

        return {"success": False, "error": f"上传状态轮询超时（{timeout_seconds}s）"}

    def _upload_file_single(self, file_path: str) -> Dict[str, Any]:
        """单文件上传."""
        if not self.global_domain:
            return {"success": False, "error": "缺少 globalDomain。"}

        path = Path(file_path)
        md5_value = self._calculate_md5(file_path)
        mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        url = f"{self.global_domain}/ous/api/v2/single/upload"

        try:
            with open(file_path, "rb") as f:
                resp = requests.post(
                    url,
                    headers=self._ous_headers(),
                    data={"md5": md5_value},
                    files={"file": (path.name, f, mime_type)},
                    timeout=300,
                    verify=self.verify_ssl,
                )
            resp.raise_for_status()
            result = resp.json()
        except requests.exceptions.RequestException as e:
            return {"success": False, "error": f"单文件上传失败: {e}", "originalPath": file_path}
        except json.JSONDecodeError as e:
            return {"success": False, "error": f"上传响应 JSON 解析失败: {e}", "originalPath": file_path}

        if not self._ok(result):
            msg = self._api_error(result)
            hint = _format_hint(msg) or _auth_hint(msg, result.get("c"))
            return {
                "success": False,
                "error": f"单文件上传失败: {msg}" + (f"\n修复建议：{hint}" if hint else ""),
                "originalPath": file_path,
            }

        poll_result = self._poll_upload_until_ready()
        poll_result["originalPath"] = file_path
        return poll_result

    def upload_file(self, file_path: str) -> Dict[str, Any]:
        """上传单个文件（自动判断单文件/分片）."""
        p = Path(file_path)
        if not p.exists():
            return {"success": False, "error": f"文件不存在: {file_path}"}

        # 单文件上传：每次上传前都重新获取 token
        if p.stat().st_size <= self.block_size:
            token_result = self.get_upload_token()
            if not token_result.get("success"):
                return token_result
            return self._upload_file_single(file_path)

        # 分片上传：需要共享同一个 token
        if not self.ous_token or not self.global_domain:
            token_result = self.get_upload_token()
            if not token_result.get("success"):
                return token_result

        return self._upload_file_block(file_path)

    def _upload_file_block(self, file_path: str) -> Dict[str, Any]:
        """分片上传."""
        if not self.global_domain:
            return {"success": False, "error": "缺少 globalDomain。"}

        path = Path(file_path)
        file_size = path.stat().st_size
        md5_value = self._calculate_md5(file_path)
        block_size = max(1, self.block_size)
        total_blocks = (file_size + block_size - 1) // block_size
        init_url = f"{self.global_domain}/ous/api/v2/block/upload/init"

        try:
            init_resp = requests.post(
                init_url,
                headers=self._ous_headers(),
                json={"md5": md5_value, "blocks": total_blocks, "size": file_size, "name": path.name},
                timeout=30,
                verify=self.verify_ssl,
            )
            init_resp.raise_for_status()
            init_result = init_resp.json()
        except requests.exceptions.RequestException as e:
            return {"success": False, "error": f"分片初始化失败: {e}", "originalPath": file_path}
        except json.JSONDecodeError as e:
            return {"success": False, "error": f"分片初始化响应解析失败: {e}", "originalPath": file_path}

        if not self._ok(init_result):
            msg = self._api_error(init_result)
            hint = _format_hint(msg) or _auth_hint(msg, init_result.get("c"))
            return {
                "success": False,
                "error": f"分片初始化失败: {msg}" + (f"\n修复建议：{hint}" if hint else ""),
                "originalPath": file_path,
            }

        init_data = init_result.get("d") or {}
        deduplicated = bool(init_data.get("deduplicated"))

        if not deduplicated:
            part_url = f"{self.global_domain}/ous/api/v2/block/upload/part"
            mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
            try:
                with open(file_path, "rb") as f:
                    for block in range(1, total_blocks + 1):
                        chunk = f.read(block_size)
                        if not chunk:
                            break
                        resp = requests.post(
                            part_url,
                            headers=self._ous_headers(),
                            data={"block": block},
                            files={"file": (f"{path.name}.part{block}", chunk, mime_type)},
                            timeout=180,
                            verify=self.verify_ssl,
                        )
                        resp.raise_for_status()
                        result = resp.json()
                        if not self._ok(result):
                            return {
                                "success": False,
                                "error": f"分片上传失败（block={block}）: {self._api_error(result)}",
                                "originalPath": file_path,
                            }
            except requests.exceptions.RequestException as e:
                return {"success": False, "error": f"分片上传失败: {e}", "originalPath": file_path}
            except json.JSONDecodeError as e:
                return {"success": False, "error": f"分片上传响应解析失败: {e}", "originalPath": file_path}

        poll_result = self._poll_upload_until_ready()
        poll_result["originalPath"] = file_path
        return poll_result

    def upload_paths(self, paths: List[str]) -> List[Dict[str, Any]]:
        """批量上传文件."""
        results: List[Dict[str, Any]] = []
        local_paths = [x for x in paths if not _is_url(x)]

        if local_paths:
            token_result = self.get_upload_token()
            if not token_result.get("success"):
                return [{"success": False, "error": token_result.get("error"), "originalPath": p} for p in paths]

        for item in paths:
            if _is_url(item):
                results.append({"success": True, "url": item, "originalPath": item, "isUrl": True})
            else:
                up = self.upload_file(item)
                results.append(up)

        return results

    # ============ 重建任务 ============

    def create_reconstruction(
        self,
        project_name: Optional[str],
        scene: str,
        resources: List[Dict[str, Any]],
        task_quality: str = "high",
        cover: Optional[str] = None,
    ) -> Dict[str, Any]:
        """创建3D重建任务.

        Args:
            project_name: 项目名称
            scene: "model" 或 "space"
            resources: 资源列表 [{"url": "...", "type": "video"/"image"}]
            task_quality: "low" | "normal" | "high"
            cover: 封面图片URL
        """
        url = f"{self.BASE_URL}{PATH_RECONSTRUCTIONS}"
        body: Dict[str, Any] = {
            "scene": scene,
            "taskQuality": task_quality,
            "resources": resources,
        }
        if project_name:
            body["name"] = project_name
        if cover:
            body["cover"] = cover

        try:
            resp = requests.post(
                url, headers=self._create_task_headers(), json=body, timeout=60, verify=self.verify_ssl
            )
            payload = self._parse_open_api_json(resp)
            err = self._check_open_api_response(resp, payload)
            if err:
                return err

            world_id = self._world_id_from_create_payload(payload)
            if not world_id:
                return {"success": False, "error": "创建成功但未返回 worldId。"}

            return {"success": True, "worldId": world_id}
        except requests.exceptions.RequestException as e:
            return {"success": False, "error": f"请求失败: {e}"}
        except ValueError as e:
            return {"success": False, "error": str(e)}

    def create_generation(
        self,
        project_name: Optional[str],
        prompt: Optional[str],
        resources: List[Dict[str, Any]],
        cover: Optional[str] = None,
    ) -> Dict[str, Any]:
        """创建3DGS世界生成任务（单张图片）.

        Args:
            project_name: 项目名称
            prompt: 生成提示词
            resources: 资源列表 [{"url": "...", "type": "image"}]
            cover: 封面图片URL
        """
        url = f"{self.BASE_URL}{PATH_GENERATIONS}"
        body: Dict[str, Any] = {}
        if project_name:
            body["name"] = project_name
        if prompt:
            body["prompt"] = prompt
        if resources:
            body["resources"] = resources
        if cover:
            body["cover"] = cover

        try:
            resp = requests.post(
                url, headers=self._create_task_headers(), json=body, timeout=60, verify=self.verify_ssl
            )
            payload = self._parse_open_api_json(resp)
            err = self._check_open_api_response(resp, payload)
            if err:
                return err

            world_id = self._world_id_from_create_payload(payload)
            if not world_id:
                return {"success": False, "error": "创建成功但未返回 worldId。"}

            return {"success": True, "worldId": world_id}
        except requests.exceptions.RequestException as e:
            return {"success": False, "error": f"请求失败: {e}"}
        except ValueError as e:
            return {"success": False, "error": str(e)}

    def get_project_info(self, world_id: str) -> Dict[str, Any]:
        """查询任务状态."""
        url = f"{self.BASE_URL}{PATH_WORLD_DETAIL.format(worldId=world_id)}"
        try:
            resp = requests.get(url, headers=self._auth_headers(), timeout=30, verify=self.verify_ssl)
            payload = self._parse_open_api_json(resp)
            err = self._check_open_api_response(resp, payload)
            if err:
                err["isTerminal"] = True
                return err

            data = payload if isinstance(payload, dict) else {}
            status = data.get("status")
            is_terminal = status in WORLD_TERMINAL_STATUS
            is_success = status == "SUCCEEDED"

            assets = data.get("assets") or {}
            splats = assets.get("splats") or {}
            urls = splats.get("urls") or {}

            return {
                "success": True,
                "worldId": data.get("worldId") or world_id,
                "task": {
                    "status": status,
                    "statusDesc": WORLD_STATUS_DESC.get(status, status or "未知"),
                    "isTerminal": is_terminal,
                    "isSuccess": is_success,
                },
                "result": {
                    "plyPath": urls.get("plyPath"),
                    "spzPath": urls.get("spzPath"),
                    "sogPath": urls.get("sogPath"),
                },
                "isTerminal": is_terminal,
            }
        except requests.exceptions.RequestException as e:
            return {"success": False, "error": f"请求失败: {e}", "isTerminal": False}
        except json.JSONDecodeError as e:
            return {"success": False, "error": f"JSON 解析错误: {e}", "isTerminal": False}

    def poll_project_until_terminal(
        self,
        world_id: str,
        interval_seconds: int = 60,
        timeout_seconds: int = 14400,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """轮询任务直到完成.

        Args:
            world_id: 世界ID
            interval_seconds: 轮询间隔（秒）
            timeout_seconds: 最大等待时间（秒）
            progress_callback: 进度回调函数
        """
        start = time.time()
        attempts = 0

        while True:
            attempts += 1
            result = self.get_project_info(world_id)

            if progress_callback:
                progress_callback(result)

            if result.get("success"):
                task = result.get("task") or {}
                if task.get("isTerminal"):
                    result["pollMeta"] = {
                        "attempts": attempts,
                        "elapsedSeconds": int(time.time() - start),
                        "intervalSeconds": interval_seconds,
                    }
                    return result
            else:
                # 查询失败但继续轮询
                pass

            elapsed = int(time.time() - start)
            if elapsed >= timeout_seconds:
                return {
                    "success": False,
                    "worldId": world_id,
                    "error": f"轮询超时（{timeout_seconds}s）",
                    "isTerminal": True,
                    "pollMeta": {
                        "attempts": attempts,
                        "elapsedSeconds": elapsed,
                        "intervalSeconds": interval_seconds,
                    },
                }

            time.sleep(max(1, interval_seconds))

    # ============ 便捷方法 ============

    def reconstruct_from_images(
        self,
        image_paths: List[str],
        project_name: Optional[str] = None,
        scene: str = "space",
        task_quality: str = "high",
        progress_callback: Optional[Callable[[str, float], None]] = None,
    ) -> Dict[str, Any]:
        """从图片进行3D重建的完整流程.

        Args:
            image_paths: 图片路径列表（至少20张）
            project_name: 项目名称
            scene: "model" 或 "space"
            task_quality: 质量等级
            progress_callback: 进度回调 (stage, progress)

        Returns:
            包含 world_id 和结果文件URL的字典
        """
        if len(image_paths) < 20:
            return {"success": False, "error": "图片数量不足，至少需要20张图片"}

        # 1. 上传图片
        if progress_callback:
            progress_callback("uploading", 0.0)

        uploads = self.upload_paths(image_paths)
        successful = [x for x in uploads if x.get("success")]
        if not successful:
            errors = [x.get("error") for x in uploads if not x.get("success")]
            return {"success": False, "error": f"上传失败: {errors}"}

        resources = [{"url": x.get("url"), "type": "image"} for x in successful]

        if progress_callback:
            progress_callback("uploading", 100.0)

        # 2. 创建重建任务
        if progress_callback:
            progress_callback("creating", 0.0)

        create_result = self.create_reconstruction(
            project_name=project_name,
            scene=scene,
            resources=resources,
            task_quality=task_quality,
        )

        if not create_result.get("success"):
            return create_result

        world_id = create_result.get("worldId")

        if progress_callback:
            progress_callback("creating", 100.0)

        # 3. 轮询等待完成
        def on_progress(result: Dict[str, Any]):
            if progress_callback and result.get("success"):
                task = result.get("task") or {}
                # 估算进度
                status = task.get("status")
                progress_map = {
                    "PENDING": 10,
                    "WAITING": 20,
                    "PREPROCESSING": 30,
                    "RUNNING": 60,
                    "SUCCEEDED": 100,
                    "FAILED": 0,
                }
                progress = progress_map.get(status, 50)
                progress_callback("processing", progress)

        final_result = self.poll_project_until_terminal(
            world_id=world_id,
            interval_seconds=30,
            progress_callback=on_progress,
        )

        return final_result

    def get_viewer_url(self, world_id: str) -> str:
        """获取查看器URL."""
        return SITE_CONFIG["viewer_url_template"].format(world_id=world_id)
