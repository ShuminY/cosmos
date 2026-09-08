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

去重策略（防止 Streamlit 重渲染/多入口重复触发）：
1. 进程内 _inflight 表：同一 DWG 路径有未完成构建时直接 join 等待，不重复 trigger。
2. 用 Jenkins queue item id 精确定位自己触发的构建，不靠 lastBuild 猜测
   （避免并发时 A 把 B 的构建结果当成自己的）。
3. 失败标记文件 .jenkins_failed：最近 N 分钟内失败过的直接跳过，避免反复重试
   产生一堆无用 job。
"""
from __future__ import annotations
import base64
import hashlib
import http.cookiejar
import json
import os
import sys
import threading
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
# 失败后多久内不重试（秒），避免短时间内刷出一堆无用 job
JENKINS_FAILURE_COOLDOWN = int(os.environ.get("JENKINS_FAILURE_COOLDOWN", "1800"))

# 全局 opener + crumb（带 cookie jar，保证 crumb 与 session 一致）
_opener: Optional[urllib.request.OpenerDirector] = None
_crumb: Optional[tuple[str, str]] = None

# 进程内去重：{dwg_path: event_wait_object}
# 同一 DWG 正在转换中时，后续调用等待前一个完成后直接读结果，不重复触发。
_inflight_lock = threading.Lock()
_inflight: dict[str, threading.Event] = {}


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


def trigger(dwg_path: str) -> Optional[int]:
    """触发参数化构建（带 CSRF crumb），成功返回 queue item id，失败返回 None.

    Jenkins 在 Location 响应头里返回 queue 项 URL，形如
    ``https://jenkins/queue/item/123/``，我们用这个 id 去定位"自己触发的"那次构建，
    而不是靠 lastBuild 猜测，避免并发时 A 把 B 的构建当自己的。
    """
    crumb = _get_crumb()
    body = urllib.parse.urlencode({JENKINS_DWG_PARAM: dwg_path}).encode()
    headers = {}
    if crumb:
        headers[crumb[0]] = crumb[1]

    def _do_trigger(extra_headers: dict) -> Optional[int]:
        url = f"{JENKINS_JOB_URL}/buildWithParameters"
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        for k, v in extra_headers.items():
            req.add_header(k, v)
        try:
            with _get_opener().open(req, timeout=30) as resp:
                location = resp.headers.get("Location", "")
                # Location: https://jenkins/queue/item/123/
                if location.rstrip("/").endswith("/api/json"):
                    location = location[:-len("/api/json")]
                parts = location.rstrip("/").rsplit("/", 1)
                if len(parts) == 2 and parts[-1].isdigit():
                    return int(parts[-1])
                # 没拿到 queue id 也算触发成功，但上层只能退回到 lastBuild 匹配
                return 0 if resp.status in (200, 201) else None
        except urllib.error.HTTPError as e:
            if e.code == 403:
                return -403  # 标记：需要重建 crumb
            return None
        except Exception:
            return None

    qid = _do_trigger(headers)
    if qid == -403:
        # crumb / session 失效，重建后重试一次
        _invalidate_session()
        crumb = _get_crumb()
        if crumb:
            qid = _do_trigger({crumb[0]: crumb[1]})
    if qid is None or qid == -403:
        return None
    return qid  # 0 = 成功但拿不到 queue id；正数 = 正常 queue item id


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


def _queue_item_build_number(queue_item_id: int) -> Optional[int]:
    """根据 queue item id 查询对应的构建号；还没出号返回 None."""
    status, body = _request(
        f"{JENKINS_URL}/queue/item/{queue_item_id}/api/json?tree=executable[number],cancelled"
    )
    if status != 200 or not body:
        return None
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    if data.get("cancelled"):
        return -1  # 已取消
    exe = data.get("executable") or {}
    num = exe.get("number")
    return int(num) if num else None


def _build_result(build_num: int) -> tuple[Optional[str], bool]:
    """查询构建结果，返回 (result, building)；result 为 None 表示查询失败."""
    status, body = _request(
        f"{JENKINS_JOB_URL}/{build_num}/api/json?tree=building,result"
    )
    if status != 200 or not body:
        return None, False
    try:
        info = json.loads(body)
        building = bool(info.get("building"))
        result = info.get("result")
        return (str(result) if result else None), building
    except (json.JSONDecodeError, ValueError, KeyError):
        return None, False


def _build_console_tail(build_num: int) -> str:
    _, tail = _request(
        f"{JENKINS_JOB_URL}/{build_num}/logText/progressiveText?start=0"
    )
    return tail.decode("utf-8", errors="replace")[-3000:]


def wait_build(queue_item_id: int = 0, baseline_number: int = 0) -> tuple[str, str]:
    """等待一次构建完成，返回 (result, console_tail).

    优先用 queue_item_id 精确定位自己触发的构建；queue_item_id 为 0 时退化
    为 baseline_number 方式（"比 baseline 大的下一次构建"，并发不安全）。

    result: SUCCESS / FAILURE / ABORTED / TIMEOUT / ERROR / CANCELLED
    console_tail: 日志末尾约 3000 字符
    """
    deadline = time.time() + JENKINS_TIMEOUT
    build_num: Optional[int] = None

    while time.time() < deadline:
        # ---------- 阶段 1：定位构建号 ----------
        if build_num is None:
            if queue_item_id > 0:
                bn = _queue_item_build_number(queue_item_id)
                if bn == -1:
                    return "CANCELLED", ""
                if bn and bn > 0:
                    build_num = bn
            else:
                # 退化路径：等 lastBuild 超过 baseline
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

        # ---------- 阶段 2：轮询构建是否完成 ----------
        result, building = _build_result(build_num)
        if result is not None and not building:
            return result, _build_console_tail(build_num)
        time.sleep(JENKINS_POLL_INTERVAL)

    return "TIMEOUT", ""


def _failure_marker_path(src_dir: Path, basename: str) -> Path:
    """失败标记文件路径（放在源目录旁边，跨进程共享冷却状态）."""
    return src_dir / f".{basename}.jenkins_failed"


def _recently_failed(dwg_path: Path) -> Optional[str]:
    """最近失败冷却中则返回错误提示，否则返回 None."""
    if JENKINS_FAILURE_COOLDOWN <= 0:
        return None
    marker = _failure_marker_path(dwg_path.parent, dwg_path.stem)
    try:
        if marker.exists():
            age = time.time() - marker.stat().st_mtime
            if age < JENKINS_FAILURE_COOLDOWN:
                left = int(JENKINS_FAILURE_COOLDOWN - age)
                m, s = divmod(left, 60)
                return (
                    f"Jenkins 最近转换失败过，{m}分{s}秒冷却后自动重试 "
                    f"（避免反复触发产生无用 job）。如需立即重试，"
                    f"删除标记文件：{marker}"
                )
    except Exception:
        pass
    return None


def _mark_failed(dwg_path: Path) -> None:
    try:
        marker = _failure_marker_path(dwg_path.parent, dwg_path.stem)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(int(time.time())))
    except Exception:
        pass


def _clear_failure(dwg_path: Path) -> None:
    try:
        marker = _failure_marker_path(dwg_path.parent, dwg_path.stem)
        if marker.exists():
            marker.unlink()
    except Exception:
        pass


def convert_and_unzip(dwg_path: Path, output_dir: Path) -> tuple[Optional[Path], str]:
    """完整一条龙：触发 Jenkins job → 等待 → 从源目录读 zip → 解压到 output_dir.

    返回 (主 PDF 路径, 错误信息)；成功时错误信息为空。
    主 PDF：zip 里找到的第一个 PDF。

    去重 & 防刷机制：
    - 同一 DWG 路径在本进程内已有进行中的转换时，后续调用 join 等待，不重复触发；
    - 最近失败冷却期内（默认 30 分钟）直接跳过，避免连续失败产生一堆无用 job；
    - 用 queue item id 精确定位"自己触发的"那次构建，并发安全。
    """
    src_dir = str(dwg_path.parent)
    basename = dwg_path.stem
    zip_name = f"{basename}.zip"
    zip_path = Path(src_dir) / zip_name

    key = str(dwg_path.resolve())

    # ---------- 0. 输出目录已有 PDF 就直接返回（上层也会判，但保险） ----------
    existing_pdf = _find_main_pdf(output_dir, basename)
    if existing_pdf:
        return existing_pdf, ""

    # ---------- 1. 最近失败冷却 ----------
    recent_err = _recently_failed(dwg_path)
    if recent_err:
        return None, recent_err

    # ---------- 2. 进程内去重：有别的线程在转同一张图，等它完事 ----------
    with _inflight_lock:
        ev = _inflight.get(key)
        if ev is not None:
            waiter = True
        else:
            ev = threading.Event()
            _inflight[key] = ev
            waiter = False

    if waiter:
        # 等前一位完成，再读结果（成功则 output_dir 里应该有 PDF）
        ev.wait(timeout=JENKINS_TIMEOUT + 30)
        # 成功的话 PDF 已在 output_dir
        existing_pdf = _find_main_pdf(output_dir, basename)
        if existing_pdf:
            return existing_pdf, ""
        # 失败的话也别再试了，直接报最近失败
        recent_err = _recently_failed(dwg_path)
        if recent_err:
            return None, recent_err
        return None, "等待其他线程的 Jenkins 转换失败"

    try:
        return _do_convert_and_unzip(dwg_path, output_dir, zip_path, basename)
    finally:
        with _inflight_lock:
            _inflight.pop(key, None)
        ev.set()


def _find_main_pdf(output_dir: Path, basename: str) -> Optional[Path]:
    """在 output_dir 里找到主 PDF，优先命名规范的 <basename>.pdf."""
    target = output_dir / f"{basename}.pdf"
    if target.exists() and target.stat().st_size > 0:
        return target
    pdfs = sorted(output_dir.glob(f"{basename}*.pdf"))
    if not pdfs:
        pdfs = sorted(output_dir.glob("*.pdf"))
    for p in pdfs:
        if p.stat().st_size > 0:
            return p
    return None


def _shared_cache_dir(dwg_path: Path) -> Path:
    """Jenkins 转换结果的共享缓存目录（所有入口共享，避免同一张图重复触发）。

    以 DWG 路径的哈希作子目录名，不管调用方把 output_dir 指到哪，都先到共享缓存
    里查；命中就直接拷过去，不用再发新的 Jenkins job。
    """
    key = str(dwg_path.resolve()).encode("utf-8")
    h = hashlib.sha1(key).hexdigest()[:16]
    return Path("data") / "jenkins_cache" / h


def _shared_cached_pdf(dwg_path: Path) -> Optional[Path]:
    """共享缓存里是否已有该 DWG 对应的主 PDF."""
    cache = _shared_cache_dir(dwg_path)
    return _find_main_pdf(cache, dwg_path.stem)


def _do_convert_and_unzip(dwg_path: Path, output_dir: Path,
                          zip_path: Path, basename: str) -> tuple[Optional[Path], str]:
    """真正执行触发 + 等待 + 解压的逻辑（被 convert_and_unzip 包裹去重/锁）."""
    # ---------- 0. 共享缓存命中：直接拷到调用方的 output_dir ----------
    cached_pdf = _shared_cached_pdf(dwg_path)
    if cached_pdf:
        return _copy_to_output(cached_pdf, output_dir, basename), ""

    # ---------- 1. zip 已经在源目录（之前别的入口触发过且成功） ----------
    if zip_path.exists() and zip_path.stat().st_size > 0:
        cache_dir = _shared_cache_dir(dwg_path)
        pdf = _unzip_and_locate(zip_path, cache_dir, basename)
        if pdf:
            _clear_failure(dwg_path)
            return _copy_to_output(pdf, output_dir, basename), ""

    # ---------- 2. 触发 Jenkins job ----------
    baseline = last_build_number() or 0

    qid = trigger(str(dwg_path))
    if qid is None:
        _mark_failed(dwg_path)
        return None, "Jenkins 构建触发失败（403 / 网络错误 / 未授权）"

    result, tail = wait_build(queue_item_id=qid, baseline_number=baseline)
    if result != "SUCCESS":
        _mark_failed(dwg_path)
        msg = f"Jenkins 构建结果: {result}"
        if tail:
            msg += "\n" + tail
        return None, msg

    if not zip_path.exists():
        _mark_failed(dwg_path)
        return None, f"构建成功但未找到 zip: {zip_path}\n{tail}"

    # 解压到共享缓存，再拷到调用方目录
    cache_dir = _shared_cache_dir(dwg_path)
    pdf = _unzip_and_locate(zip_path, cache_dir, basename)
    if pdf is None:
        _mark_failed(dwg_path)
        return None, "zip 里没有 PDF 文件"

    _clear_failure(dwg_path)
    return _copy_to_output(pdf, output_dir, basename), ""


def _merge_pdfs(pdf_paths: list[Path], target: Path) -> Optional[Path]:
    """把多个 PDF 按顺序合并成一个多页 PDF，写到 target，成功返回 target."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        try:
            import pymupdf as fitz  # type: ignore
        except ImportError:
            return None
    try:
        out = fitz.open()
        try:
            for p in pdf_paths:
                doc = fitz.open(str(p))
                try:
                    out.insert_pdf(doc)
                finally:
                    doc.close()
            # 写到临时文件再改名，避免合并到一半失败留下坏文件
            tmp = target.with_suffix(".pdf.tmp")
            out.save(str(tmp))
            tmp.replace(target)
        finally:
            out.close()
        return target if target.exists() and target.stat().st_size > 0 else None
    except Exception:
        return None


def _copy_to_output(src_pdf: Path, output_dir: Path, basename: str) -> Optional[Path]:
    """把共享缓存里的 PDF 拷到调用方指定的 output_dir，返回目标路径."""
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{basename}.pdf"
    try:
        if src_pdf.resolve() != target.resolve() and src_pdf.exists():
            import shutil
            shutil.copy2(str(src_pdf), str(target))
    except Exception:
        # 拷失败至少返回源路径，调用方还能用
        return src_pdf
    return target if target.exists() else src_pdf


def _unzip_and_locate(zip_path: Path, output_dir: Path,
                      basename: str) -> Optional[Path]:
    """解压 zip，返回合并后的多页 PDF 路径。

    Jenkins 出的 zip 里可能每个 layout 一个 PDF（共几十页），和 ezdxf 路径
    的"单个多页 PDF"格式不一致。这里把所有 PDF 按文件名排序后合并成一个
    <basename>.pdf，保证上层（页面预览 / PNG 拆页 / SVG 导出）不管走哪条
    路径拿到的都是统一格式的多页 PDF。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        import zipfile
        with zipfile.ZipFile(str(zip_path), "r") as zf:
            zf.extractall(str(output_dir))
    except Exception:
        return None

    # 收集所有 PDF（递归，防止 zip 里多了一层目录）
    all_pdfs: list[Path] = []
    for p in sorted(output_dir.rglob("*.pdf"), key=lambda x: x.name):
        if p.stat().st_size > 0:
            all_pdfs.append(p)
    if not all_pdfs:
        return None

    target = output_dir / f"{basename}.pdf"

    # 单文件且本身就是目标名，直接返回
    if len(all_pdfs) == 1 and all_pdfs[0].resolve() == target.resolve():
        return target

    # 多文件合并成一个多页 PDF（和 ezdxf 路径输出格式对齐）
    if len(all_pdfs) > 1:
        merged = _merge_pdfs(all_pdfs, target)
        if merged:
            return merged

    # 合并失败 / 只有一个：退化为拷贝/重命名第一个
    found = all_pdfs[0]
    if found != target and found.exists():
        try:
            import shutil
            shutil.copy2(str(found), str(target))
        except Exception:
            pass
    # 优先返回命名规范的那个
    return target if target.exists() else main_pdf
