#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Aholo 3D Reconstruction Skill (OpenAPI v1)

流程（Aholo 开放平台，网关 https://api.aholo3d.cn，路径以 @RestApi#openApiUrl 为准）：
1) 获取上传凭证：GET /world/v1/asset/token（成功直出凭证对象）
2) 本地文件上传到 OUS globalDomain（仍为 OUS V2 的 c/m/d 封装）
3) 创建重建/生成：POST `/world/v1/reconstructions` 或 `/world/v1/generations`（成功时为 JSON 对象 `WorldAsyncOperation`，字段 `worldId`；网关/旧版偶发裸文本时脚本仍兼容）
4) 查询/轮询：GET /world/v1/{worldId}（成功直出世界详情对象）
"""

import hashlib
import json
import mimetypes
import re
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import urllib3


SITE_CONFIG = {
    "base_url": "https://api.aholo3d.cn",
    "path_prefix": "",
    "viewer_url_template": "https://studio.aholo3d.cn/3dgs-model/{world_id}",
    "api_keys_url": "https://labs.aholo3d.cn/api-keys",
    "skill_script_path": ".cursor/skills/aholo-3dgs-reconstruction/aholo_reconstruct.py",
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


@contextmanager
def step_timer(step_name: str):
    start = time.time()
    print(f"[开始] {step_name}")
    try:
        yield
    finally:
        elapsed = time.time() - start
        print(f"[结束] {step_name} | 耗时: {elapsed:.2f}s")


def _is_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


def _auth_hint(error_msg: str, code: Optional[str] = None) -> str:
    text = f"{error_msg} {code or ''}".lower()
    if any(x in text for x in ["auth", "authorization", "401", "403", "appkey", "api key", "apikey", "鉴权", "认证"]):
        return "请检查 `AHOLO_API_KEY` 是否正确，并确保请求头 `Authorization` 直接传 API key（无 Bearer 前缀）。"
    return ""


def _format_hint(error_msg: str) -> str:
    msg = error_msg.lower()
    if "格式" in error_msg or "format" in msg or "h.264" in msg or "codec" in msg:
        return "请确保视频为标准 mp4 且编码为 H.264，可先用 ffprobe 检查并用 ffmpeg 转码。"
    return ""


class AholoClient:
    BASE_URL = SITE_CONFIG["base_url"]

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
        """解析开放平台 JSON 响应；兼容 UTF-8 BOM、首尾空白、合法 JSON 后的尾随空白/杂质。"""
        text = (resp.text or "").strip().lstrip("\ufeff")
        if not text:
            raise ValueError("响应体为空")
        try:
            decoder = json.JSONDecoder()
            obj, end = decoder.raw_decode(text)
            return obj
        except json.JSONDecodeError:
            # 规范成功体为 JSON 对象 {"worldId": "..."}；少数网关/旧版可能仍返回裸 worldId 文本，此处兜底。
            if re.fullmatch(r"[A-Za-z0-9_-]{4,200}", text):
                return text
            raise ValueError(f"响应解析失败: 非合法 JSON，且正文不是可识别的 worldId 文本") from None

    @staticmethod
    def _world_id_from_create_payload(payload: Any) -> Optional[str]:
        """从创建接口成功响应中取出 worldId（WorldAsyncOperation JSON 或历史兼容形态）。"""
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

    def __init__(self, api_key: str):
        self.api_key = api_key
        skip_verify = str(os.environ.get("AHOLO_INSECURE_SKIP_VERIFY", "")).strip().lower()
        force_verify = str(os.environ.get("AHOLO_FORCE_SSL_VERIFY", "")).strip().lower()
        if force_verify in {"1", "true", "yes", "on"}:
            self.verify_ssl = True
        elif skip_verify in {"0", "false", "no", "off"}:
            self.verify_ssl = True
        else:
            # 默认自动绕过证书校验，避免公司/本机自签证书导致调用失败。
            self.verify_ssl = False
        if not self.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.ous_token: Optional[str] = None
        self.global_domain: Optional[str] = None
        self.block_size: int = 1024 * 1024

    @staticmethod
    def _ok(result: Dict[str, Any]) -> bool:
        """OUS V2 接口仍使用 c/m/d 封装。"""
        return str(result.get("c")) == "0"

    @staticmethod
    def _api_error(result: Dict[str, Any], default: str = "Unknown error") -> str:
        """OUS V2 错误信息。"""
        return str(result.get("m") or default)

    def _auth_headers(self) -> Dict[str, str]:
        return {"Authorization": self.api_key, "Content-Type": "application/json"}

    def _create_task_headers(self) -> Dict[str, str]:
        """重建/生成创建接口：附带 x-source=skills，平台记为 OPEN_API_SKILL。"""
        headers = self._auth_headers()
        headers[HEADER_X_SOURCE] = X_SOURCE_VALUE_SKILLS
        return headers

    def _ous_headers(self) -> Dict[str, str]:
        if not self.ous_token:
            return {}
        return {"ous-token-v2": self.ous_token}

    def get_upload_token(self) -> Dict[str, Any]:
        url = f"{self.BASE_URL}{PATH_UPLOAD_TOKEN}"
        with step_timer("获取上传 token"):
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

    def _poll_upload_until_ready(
        self, timeout_seconds: int = 120, interval_seconds: float = 0.5
    ) -> Dict[str, Any]:
        if not self.global_domain or not self.ous_token:
            return {"success": False, "error": "缺少上传凭证，无法轮询上传状态。"}

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
                    "error": f"上传失败，status={status} errorCode={data.get('errorCode')} errorMsg={data.get('errorMsg')}",
                }
            time.sleep(max(0.2, interval_seconds))

        return {"success": False, "error": f"上传状态轮询超时（{timeout_seconds}s）"}

    def _upload_file_single(self, file_path: str) -> Dict[str, Any]:
        if not self.global_domain:
            return {"success": False, "error": "缺少 globalDomain。"}

        path = Path(file_path)
        md5_value = self._calculate_md5(file_path)
        mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        url = f"{self.global_domain}/ous/api/v2/single/upload"

        with step_timer(f"单文件上传: {path.name}"):
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

    def _upload_file_block(self, file_path: str) -> Dict[str, Any]:
        if not self.global_domain:
            return {"success": False, "error": "缺少 globalDomain。"}

        path = Path(file_path)
        file_size = path.stat().st_size
        md5_value = self._calculate_md5(file_path)
        block_size = max(1, self.block_size)
        total_blocks = (file_size + block_size - 1) // block_size
        init_url = f"{self.global_domain}/ous/api/v2/block/upload/init"

        with step_timer(f"分片上传: {path.name}"):
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
                if not self._ok(init_result) and "md5" in str(self._api_error(init_result)).lower():
                    # beta 环境兼容：部分网关实现要求分片 init 参数走 query。
                    init_resp = requests.post(
                        init_url,
                        headers=self._ous_headers(),
                        params={"md5": md5_value, "blocks": total_blocks, "size": file_size, "name": path.name},
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

    def upload_file(self, file_path: str) -> Dict[str, Any]:
        p = Path(file_path)
        if not p.exists():
            return {"success": False, "error": f"文件不存在: {file_path}"}

        # 单文件上传：每次上传前都重新获取 token（token 是一次性的）
        if p.stat().st_size <= self.block_size:
            token_result = self.get_upload_token()
            if not token_result.get("success"):
                return token_result
            return self._upload_file_single(file_path)

        # 分片上传：需要多个步骤共享同一个 token
        if not self.ous_token or not self.global_domain:
            token_result = self.get_upload_token()
            if not token_result.get("success"):
                return token_result
        return self._upload_file_block(file_path)

    def upload_paths(self, paths: List[str], input_label: str = "素材") -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        local_paths = [x for x in paths if not _is_url(x)]
        if local_paths:
            token_result = self.get_upload_token()
            if not token_result.get("success"):
                return [{"success": False, "error": token_result.get("error"), "originalPath": p} for p in paths]

        for item in paths:
            if _is_url(item):
                print(f"使用已有 URL（跳过上传）: {item}")
                results.append({"success": True, "url": item, "originalPath": item, "isUrl": True})
            else:
                print(f"正在上传{input_label}: {item}")
                up = self.upload_file(item)
                if up.get("success"):
                    print(f"{input_label}上传成功: {item}")
                else:
                    print(f"{input_label}上传失败: {item} - {up.get('error')}")
                results.append(up)
        return results

    def create_reconstruction(
        self,
        project_name: Optional[str],
        scene: str,
        resources: List[Dict[str, Any]],
        task_quality: str = "high",
        cover: Optional[str] = None,
    ) -> Dict[str, Any]:
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
        with step_timer("创建重建任务"):
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
        with step_timer("创建生成任务"):
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
        url = f"{self.BASE_URL}{PATH_WORLD_DETAIL.format(worldId=world_id)}"
        with step_timer(f"查询任务状态: {world_id}"):
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
                urls = (((data.get("assets") or {}).get("splats") or {}).get("urls") or {})
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

    def poll_project_until_terminal(self, world_id: str, interval_seconds: int = 60, timeout_seconds: int = 14400) -> Dict[str, Any]:
        start = time.time()
        attempts = 0
        history: List[Dict[str, Any]] = []

        while True:
            attempts += 1
            result = self.get_project_info(world_id)
            now = int(time.time())

            if result.get("success"):
                task = result.get("task") or {}
                history.append({"ts": now, "status": task.get("status")})
                print(f"[poll #{attempts}] status={task.get('status')} ({task.get('statusDesc')})")
                if task.get("isTerminal"):
                    result["pollMeta"] = {
                        "attempts": attempts,
                        "elapsedSeconds": int(time.time() - start),
                        "intervalSeconds": interval_seconds,
                        "recentHistory": history[-5:],
                    }
                    return result
            else:
                history.append({"ts": now, "error": result.get("error")})
                print(f"[poll #{attempts}] query failed: {result.get('error')}")

            elapsed = int(time.time() - start)
            if elapsed >= timeout_seconds:
                return {
                    "success": False,
                    "worldId": world_id,
                    "error": f"轮询超时（{timeout_seconds}s）。可稍后继续用 status/poll 查询。",
                    "isTerminal": True,
                    "pollMeta": {
                        "attempts": attempts,
                        "elapsedSeconds": elapsed,
                        "intervalSeconds": interval_seconds,
                        "recentHistory": history[-5:],
                    },
                }
            time.sleep(max(1, interval_seconds))


def format_create_result(
    result: Dict[str, Any],
    skill_script_path: Optional[str] = None,
) -> str:
    if not result.get("success"):
        return "## 任务创建失败\n\n" + f"**错误:** {result.get('error', 'Unknown error')}"
    world_id = result.get("worldId")
    cfg = SITE_CONFIG
    script_path = skill_script_path or cfg["skill_script_path"]
    viewer_url = cfg["viewer_url_template"].format(world_id=world_id)
    lines = [
        "## 任务创建成功",
        "",
        f"**世界 ID (worldId):** `{world_id}`",
        f"**查看地址:** {viewer_url}",
        "",
        "后续建议直接使用 `poll`：",
        "```bash",
        f"python3 {script_path} '{{\"action\":\"poll\",\"worldId\":\"{world_id}\",\"intervalSeconds\":60,\"timeoutSeconds\":14400}}'",
        "```",
    ]
    return "\n".join(lines)


def format_status_result(result: Dict[str, Any], world_id: str) -> str:
    if not result.get("success"):
        return "\n".join(
            ["## 项目状态查询失败", "", f"**世界 ID:** `{world_id}`", f"**错误:** {result.get('error', 'Unknown error')}"]
        )

    task = result.get("task") or {}
    status = task.get("status")
    is_terminal = task.get("isTerminal")
    is_success = task.get("isSuccess")
    title = "## 任务完成 - 成功" if is_terminal and is_success else f"## 任务状态: {task.get('statusDesc')}"
    lines = [
        title,
        "",
        f"**世界 ID:** `{result.get('worldId')}`",
        f"**状态:** `{status}`",
        f"**是否终态:** {is_terminal}",
    ]

    if is_terminal and is_success:
        data = result.get("result") or {}
        lines.extend(
            [
                "",
                "### 结果文件",
                "",
                f"- **PLY 文件:** {data.get('plyPath') or '无'}",
                f"- **SPZ 文件:** {data.get('spzPath') or '无'}",
                f"- **SOG 文件:** {data.get('sogPath') or '无'}",
            ]
        )

    if is_terminal:
        lines.extend(["", "*任务已结束，轮询已停止。*"])
    return "\n".join(lines)


def format_poll_result(result: Dict[str, Any], world_id: str) -> str:
    lines = ["## 轮询结果", ""]
    meta = result.get("pollMeta") or {}
    lines.extend(
        [
            f"**世界 ID:** `{world_id}`",
            f"**轮询次数:** {meta.get('attempts', 0)}",
            f"**耗时(秒):** {meta.get('elapsedSeconds', 0)}",
            f"**轮询间隔(秒):** {meta.get('intervalSeconds', 0)}",
            "",
        ]
    )
    if result.get("success"):
        lines.append(format_status_result(result, world_id))
    else:
        lines.extend(["## 轮询失败", "", f"**错误:** {result.get('error', 'Unknown error')}"])
    return "\n".join(lines)


def parse_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return default


def main() -> None:
    if len(sys.argv) < 2:
        print("## 使用说明")
        print("")
        print("支持操作：create | create-reconstruction | create-generation | status | poll")
        print("环境变量仅需：`AHOLO_API_KEY`")
        print("")
        print("重建任务参数：")
        print("  - `videoPaths`: 视频文件列表（1-3个）")
        print("  - `imagePaths`: 图片文件列表（至少20张）")
        print("  - `imageDir`: 图片目录（自动扫描目录下所有图片，与 imagePaths 二选一）")
        print("  - `scene`: `model` 或 `space`")
        print("  - `taskQuality`: `low` | `normal` | `high`")
        sys.exit(1)

    try:
        params = json.loads(sys.argv[1])
    except json.JSONDecodeError as e:
        print("## JSON 解析错误\n")
        print(f"**详情:** {e}")
        sys.exit(1)

    api_key = os.environ.get("AHOLO_API_KEY", "").strip()
    if not api_key:
        print("## 缺少环境变量\n")
        print("请在本机为当前环境配置 `AHOLO_API_KEY`（Aholo 开放平台 API key）。")
        print(f"若尚未创建 API key，请前往 {SITE_CONFIG['api_keys_url']}")
        print("")
        print("配置完成后，**回到 Cursor 对话中回复「继续」**或重新说明你的 3D 任务即可，由 Agent 代为执行脚本与后续步骤。")
        print("在未配置 API key 时，**请勿自行反复运行本脚本**尤其是创建类 action，以免产生混乱。")
        sys.exit(1)

    client = AholoClient(api_key=api_key)
    action = params.get("action", "create")
    forbid_create = parse_bool(params.get("forbidCreate"), False)
    create_actions = {"create", "create-reconstruction", "create-generation"}
    if forbid_create and action in create_actions:
        print("## 已拦截：禁止重新创建任务\n")
        print("当前请求包含 `forbidCreate=true`，请改用 `status` 或 `poll`。")
        sys.exit(1)

    if action in create_actions:
        workflow = params.get("workflow", "reconstruction")
        if action == "create-reconstruction":
            workflow = "reconstruction"
        elif action == "create-generation":
            workflow = "generation"
        workflow = str(workflow).strip().lower()
        if workflow not in {"reconstruction", "generation"}:
            print("## workflow 无效\n")
            print("`workflow` 仅支持 `reconstruction` 或 `generation`。")
            sys.exit(1)

        project_name_raw = params.get("projectName")
        if project_name_raw is None:
            project_name: Optional[str] = None
        else:
            project_name = str(project_name_raw).strip() or None

        print("## 创建 Aholo 3DGS 任务\n")
        if project_name:
            print(f"项目名: {project_name}")
        else:
            print("项目名: （未指定，由服务端默认）")

        cover_url = params.get("cover")
        cover_path = params.get("coverPath")
        if cover_path:
            print("### 步骤 1: 上传封面（可选）")
            cover_up = client.upload_file(cover_path)
            if not cover_up.get("success"):
                print("\n## 封面上传失败")
                print(f"**错误:** {cover_up.get('error')}")
                sys.exit(1)
            cover_url = cover_up.get("url")

        if workflow == "reconstruction":
            scene = params.get("scene")
            task_quality = params.get("taskQuality")
            if not scene or scene not in {"model", "space"}:
                print("## 缺少或无效参数：scene\n")
                print("请确认是空间重建还是模型重建：`scene` 只能是 `model` 或 `space`。")
                sys.exit(1)
            if not task_quality or task_quality not in {"low", "normal", "high"}:
                print("## 缺少或无效参数：taskQuality\n")
                print("请确认质量等级：`taskQuality` 只能是 `low`/`normal`/`high`。")
                sys.exit(1)

            video_paths = params.get("videoPaths") or []
            image_paths = params.get("imagePaths") or []
            image_dir = params.get("imageDir")

            # 处理 imageDir：自动扫描目录下的图片
            if image_dir:
                if image_paths:
                    print("## 参数冲突\n")
                    print("请二选一：`imageDir`（目录）或 `imagePaths`（文件列表）。")
                    sys.exit(1)
                if not os.path.isdir(image_dir):
                    print("## 目录不存在\n")
                    print(f"`imageDir` 指定的目录不存在：`{image_dir}`")
                    sys.exit(1)
                valid_ext = ('.jpg', '.jpeg', '.png', '.bmp', '.webp', '.gif')
                image_paths = [
                    os.path.join(image_dir, f)
                    for f in os.listdir(image_dir)
                    if f.lower().endswith(valid_ext)
                ]
                image_paths.sort()
                print(f"### 从目录扫描到 {len(image_paths)} 张图片")

            if video_paths and image_paths:
                print("## 参数冲突\n")
                print("重建任务请二选一：`videoPaths` 或 `imagePaths`/`imageDir`。")
                sys.exit(1)
            if not video_paths and not image_paths:
                print("## 缺少输入资源\n")
                print("重建任务需要提供 `videoPaths`、`imagePaths` 或 `imageDir`。")
                sys.exit(1)
            if video_paths and (len(video_paths) < 1 or len(video_paths) > 3):
                print("## 视频数量无效\n")
                print("`videoPaths` 需要 1-3 个元素。")
                sys.exit(1)
            if image_paths and len(image_paths) < 20:
                print("## 图片数量不足\n")
                print(f"`imagePaths` 至少需要 20 张图片，当前只有 {len(image_paths)} 张。")
                sys.exit(1)

            resources: List[Dict[str, Any]] = []
            if video_paths:
                print("### 步骤 1: 上传视频")
                uploads = client.upload_paths(video_paths, "视频")
                successful = [x for x in uploads if x.get("success")]
                if not successful:
                    print("\n## 上传失败")
                    for r in uploads:
                        print(f"- `{r.get('originalPath')}`: {r.get('error')}")
                    sys.exit(1)
                resources.extend([{"url": x.get("url"), "type": "video"} for x in successful])
            if image_paths:
                print("### 步骤 1: 上传图片")
                uploads = client.upload_paths(image_paths, "图片")
                successful = [x for x in uploads if x.get("success")]
                if not successful:
                    print("\n## 上传失败")
                    for r in uploads:
                        print(f"- `{r.get('originalPath')}`: {r.get('error')}")
                    sys.exit(1)
                resources.extend([{"url": x.get("url"), "type": "image"} for x in successful])

            print("\n### 步骤 2: 创建重建项目")
            create_result = client.create_reconstruction(
                project_name=project_name,
                scene=scene,
                resources=resources,
                task_quality=task_quality,
                cover=cover_url,
            )
        else:
            image_paths = params.get("imagePaths") or []
            prompt = params.get("prompt")
            if len(image_paths) > 1:
                print("## 图片数量无效\n")
                print("生成任务（spatial gen）`imagePaths` 最多只能 1 张。")
                sys.exit(1)
            if not image_paths and not prompt:
                print("## 缺少输入\n")
                print("生成任务要求 `imagePaths` 与 `prompt` 不能同时为空。")
                sys.exit(1)
            if params.get("videoPaths"):
                print("## 参数无效\n")
                print("生成任务不支持 `videoPaths`，请使用 `imagePaths`。")
                sys.exit(1)

            resources: List[Dict[str, Any]] = []
            if image_paths:
                print("### 步骤 1: 上传图片")
                uploads = client.upload_paths(image_paths, "图片")
                successful = [x for x in uploads if x.get("success")]
                if not successful:
                    print("\n## 上传失败")
                    for r in uploads:
                        print(f"- `{r.get('originalPath')}`: {r.get('error')}")
                    sys.exit(1)
                resources.extend([{"url": x.get("url"), "type": "image"} for x in successful])

            print("\n### 步骤 2: 创建生成项目（spatial gen）")
            create_result = client.create_generation(
                project_name=project_name,
                prompt=prompt,
                resources=resources,
                cover=cover_url,
            )

        if not create_result.get("success"):
            print("\n## 项目创建失败")
            print(f"**错误:** {create_result.get('error')}")
            sys.exit(1)
        print("\n" + format_create_result(create_result))
        return

    if action == "status":
        world_id = params.get("worldId")
        if not world_id:
            print("## 缺少必需参数\n")
            print("- **worldId**: 世界任务 ID")
            sys.exit(1)
        result = client.get_project_info(world_id)
        print(format_status_result(result, world_id))
        return

    if action == "poll":
        world_id = params.get("worldId")
        if not world_id:
            print("## 缺少必需参数\n")
            print("- **worldId**: 世界任务 ID")
            sys.exit(1)
        interval_seconds = int(params.get("intervalSeconds", 60))
        timeout_seconds = int(params.get("timeoutSeconds", 14400))
        if interval_seconds <= 0:
            interval_seconds = 60
        if timeout_seconds <= 0:
            timeout_seconds = 14400
        result = client.poll_project_until_terminal(
            world_id=world_id,
            interval_seconds=interval_seconds,
            timeout_seconds=timeout_seconds,
        )
        print(format_poll_result(result, world_id))
        return

    print("## 无效的操作\n")
    print(
        f"**action** 必须是 `create`、`create-reconstruction`、`create-generation`、`status` 或 `poll`，当前值: `{action}`"
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
