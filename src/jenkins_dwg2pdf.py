"""Jenkins dwg2pdf job 客户端。

封装触发 / 轮询 / 读取 Jenkins dwg2pdf job 的通用逻辑，供 views_dwg2pdf.py 和
views_drawings.py 等多个页面复用。

配置（环境变量，全部配了才启用）：
    JENKINS_URL      = https://jenkins.example.com/
    JENKINS_USER     = jenkins
    JENKINS_TOKEN    = API token
    JENKINS_JOB_URL  = https://jenkins.example.com/job/build/job/dwg2pdf/
    JENKINS_DWG_PARAM = downLoadPath   （可选，默认 downLoadPath）
    JENKINS_POLL_INTERVAL = 10         （秒，可选）
    JENKINS_TIMEOUT       = 600        （秒，可选）

job 约定：传入 dwg 的服务器绝对路径 → job 跑完后在源目录下生成同名 .zip。
"""
from __future__ import annotations
import base64
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional


JENKINS_URL = os.environ.get("JENKINS_URL", "").rstrip("/")
JENKINS_USER = os.environ.get("JENKINS_USER", "")
JENKINS_TOKEN = os.environ.get("JENKINS_TOKEN", "")
JENKINS_JOB_URL = os.environ.get("JENKINS_JOB_URL", "").rstrip("/")
JENKINS_DWG_PARAM = os.environ.get("JENKINS_DWG_PARAM", "downLoadPath")
JENKINS_POLL_INTERVAL = int(os.environ.get("JENKINS_POLL_INTERVAL", "10"))
JENKINS_TIMEOUT = int(os.environ.get("JENKINS_TIMEOUT", "600"))

# 全局 opener + crumb（带 cookie jar，保证 crumb 与 session 一致）
_opener: Optional[urllib.request.OpenerDirector] = None
_crumb: Optional[tuple[str, str]] = None


def available() -> bool:
    """Jenkins 配置齐全且非 macOS 时可用."""
    return bool(
        JENKINS_URL and JENKINS_USER and JENKINS_TOKEN and JENKINS_JOB_URL
    ) and sys.platform != "darwin"


def _get_opener() -> urllib.request.OpenerDirector:
    global _opener
    if _opener is not None:
        return _opener
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    auth = base64.b64encode(f"{JENKINS_USER}:{JENKINS_TOKEN}".encode()).decode()
    opener.addheaders = [("Authorization", f"Basic {auth}")]
    _opener = opener
    return opener


def _request(path: str, method: str = "GET",
             data: Optional[bytes] = None,
             extra_headers: Optional[dict] = None) -> tuple[int, bytes]:
    url = f"{JENKINS_URL}{path}" if path.startswith("/") else path
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if extra_headers:
        for k, v in extra_headers.items():
            req.add_header(k, v)
    try:
        with _get_opener().open(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""
    except Exception:
        return 0, b""


def _get_crumb() -> Optional[tuple[str, str]]:
    global _crumb
    if _crumb is not None:
        return _crumb
    status, body = _request("/crumbIssuer/api/json")
    if status == 200 and body:
        try:
            data = json.loads(body)
            name = data.get("crumbRequestField") or "Jenkins-Crumb"
            value = data.get("crumb")
            if value:
                _crumb = (name, value)
                return _crumb
        except (json.JSONDecodeError, ValueError, KeyError):
            pass
    return None


def _invalidate_session() -> None:
    """crumb / session 失效时清空，下次重建."""
    global _opener, _crumb
    _opener = None
    _crumb = None


def trigger(dwg_path: str) -> bool:
    """触发参数化构建（带 CSRF crumb），成功返回 True."""
    crumb = _get_crumb()
    body = urllib.parse.urlencode({JENKINS_DWG_PARAM: dwg_path}).encode()
    headers = {}
    if crumb:
        headers[crumb[0]] = crumb[1]
    status, _ = _request(
        f"{JENKINS_JOB_URL}/buildWithParameters",
        method="POST", data=body, extra_headers=headers,
    )
    if status == 403:
        # crumb / session 失效，重建后重试一次
        _invalidate_session()
        crumb = _get_crumb()
        if crumb:
            headers2 = {crumb[0]: crumb[1]}
            status, _ = _request(
                f"{JENKINS_JOB_URL}/buildWithParameters",
                method="POST", data=body, extra_headers=headers2,
            )
    return status in (200, 201)


def last_build_number() -> Optional[int]:
    """最近一次构建号，失败返回 None."""
    status, body = _request(f"{JENKINS_JOB_URL}/api/json?tree=lastBuild[number]")
    if status == 200 and body:
        try:
            lb = json.loads(body).get("lastBuild") or {}
            n = lb.get("number")
            return int(n) if n else None
        except (json.JSONDecodeError, ValueError, KeyError):
            pass
    return None


def wait_build(baseline_number: int) -> tuple[str, str]:
    """等待一次新构建完成，返回 (result, console_tail).

    result: SUCCESS / FAILURE / ABORTED / TIMEOUT / ERROR
    console_tail: 日志末尾约 3000 字符
    """
    deadline = time.time() + JENKINS_TIMEOUT
    build_num: Optional[int] = None
    while time.time() < deadline:
        if build_num is None:
            status, body = _request(
                f"{JENKINS_JOB_URL}/api/json?tree=lastBuild[number,building,result]"
            )
            if status == 200 and body:
                try:
                    lb = json.loads(body).get("lastBuild") or {}
                    num = int(lb.get("number") or 0)
                    if num > baseline_number:
                        build_num = num
                except (json.JSONDecodeError, ValueError, KeyError):
                    pass
            if build_num is None:
                time.sleep(JENKINS_POLL_INTERVAL)
                continue
        # 轮询构建是否完成
        status, body = _request(
            f"{JENKINS_JOB_URL}/{build_num}/api/json?tree=building,result"
        )
        if status == 200 and body:
            try:
                info = json.loads(body)
                if info.get("building"):
                    time.sleep(JENKINS_POLL_INTERVAL)
                    continue
                result = str(info.get("result") or "UNKNOWN")
                _, tail = _request(
                    f"{JENKINS_JOB_URL}/{build_num}/logText/progressiveText?start=0"
                )
                tail_text = tail.decode("utf-8", errors="replace")[-3000:]
                return result, tail_text
            except (json.JSONDecodeError, ValueError, KeyError):
                pass
        time.sleep(JENKINS_POLL_INTERVAL)
    return "TIMEOUT", ""


def convert_and_unzip(dwg_path: Path, output_dir: Path) -> tuple[Optional[Path], str]:
    """完整一条龙：触发 Jenkins job → 等待 → 从源目录读 zip → 解压到 output_dir.

    返回 (主 PDF 路径, 错误信息)；成功时错误信息为空。
    主 PDF：zip 里找到的第一个 PDF。
    """
    src_dir = str(dwg_path.parent)
    basename = dwg_path.stem
    zip_name = f"{basename}.zip"
    zip_path = Path(src_dir) / zip_name

    baseline = last_build_number() or 0

    if not trigger(str(dwg_path)):
        return None, "Jenkins 构建触发失败（403 / 网络错误 / 未授权）"

    result, tail = wait_build(baseline)
    if result != "SUCCESS":
        msg = f"Jenkins 构建结果: {result}"
        if tail:
            msg += "\n" + tail
        return None, msg

    if not zip_path.exists():
        return None, f"构建成功但未找到 zip: {zip_path}\n{tail}"

    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        import zipfile
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(str(output_dir))
    except Exception as e:
        return None, f"zip 解压失败: {e}"

    pdfs = sorted(output_dir.glob(f"{basename}*.pdf"))
    if not pdfs:
        pdfs = sorted(output_dir.glob("*.pdf"))
    if not pdfs:
        return None, "zip 里没有 PDF 文件"

    main_pdf = pdfs[0]
    target = output_dir / f"{basename}.pdf"
    if main_pdf != target and main_pdf.exists():
        try:
            import shutil
            shutil.copy2(str(main_pdf), str(target))
        except Exception:
            pass
    # 优先返回命名规范的那个
    final = target if target.exists() else main_pdf
    return final, ""
