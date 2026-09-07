"""DWG 转 PDF 页面 - 把上传的 DWG 转成 PDF，页面预览 PDF，原始 DWG 仅供下载。

平台自适应：
- Linux/Windows 服务器：
    1. 优先走 Jenkins dwg2pdf job（配置了 JENKINS_* 环境变量时）
    2. ODA File Converter CLI → ezdxf 渲染（一条龙全自动）
    3. 失败回退 LibreOffice
- macOS 本地：ODA 是 GUI 应用无法 CLI，依赖用户在缓存目录中放入已转好的 DXF
"""
from __future__ import annotations
import base64
import http.cookiejar
import io
import os
import platform
import urllib.request
import urllib.error
import urllib.parse
import json
import time
import zipfile
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import streamlit as st

from src.db import session, Document, Project
from src.storage import documents_dir, project_dir
from src.time_utils import beijing_timestamp, format_beijing

# PDF 拆页依赖：PyMuPDF
try:
    import fitz  # PyMuPDF
    PYMUPDF_AVAILABLE = True
except ImportError:
    fitz = None
    PYMUPDF_AVAILABLE = False

# DXF → PDF 多 layout 渲染（用 ezdxf 保留每张图纸真实尺寸）
try:
    import ezdxf
    from ezdxf.addons.drawing import RenderContext, Frontend
    from ezdxf.addons.drawing.pymupdf import PyMuPdfBackend
    EZDXF_AVAILABLE = True
except ImportError:
    ezdxf = None
    EZDXF_AVAILABLE = False


# DXF 缓存根目录：macOS 用户把 DWG 在 ODA GUI 里转出 DXF 后放在这里对应子目录
DXF_CACHE_ROOT = Path("data") / "dwg_dxf_cache"

# ---------- Jenkins DWG→PDF job ----------
# 通用逻辑抽到 src.jenkins_dwg2pdf 模块，供 views_dwg2pdf / views_drawings 等复用。
# 配置通过环境变量 JENKINS_URL / JENKINS_USER / JENKINS_TOKEN / JENKINS_JOB_URL 控制。
try:
    from src import jenkins_dwg2pdf as _jd
    _JENKINS_AVAILABLE = _jd.available()
except Exception:
    _jd = None  # type: ignore
    _JENKINS_AVAILABLE = False

def _jenkins_available() -> bool:
    return bool(_JENKINS_AVAILABLE and not _is_macos())

def _convert_dwg_to_pdf_via_jenkins(dwg_path: Path, pdf_out: Path) -> tuple[bool, str]:
    """走 Jenkins dwg2pdf job 转换，完成后从源目录读 zip 并解压到 pdf_out."""
    if not _jd:
        return False, "Jenkins 模块未加载"
    pdf_path, err = _jd.convert_and_unzip(dwg_path, pdf_out.parent)
    if pdf_path is None:
        return False, err
    if pdf_path != pdf_out:
        try:
            shutil.copy2(str(pdf_path), str(pdf_out))
        except Exception as e:
            return False, f"PDF 重命名失败: {e}"
    return True, ""




def _user_id() -> int | None:
    """获取当前用户ID."""
    user = st.session_state.get("user") or {}
    return user.get("id")


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _doc_file_path(project_id: int, doc: Document) -> Path:
    """获取图纸文件绝对路径."""
    return project_dir(project_id) / doc.path


def _dwg2pdf_dir(project_id: int, doc_id: int) -> Path:
    """DWG→PDF 转换结果目录."""
    out_dir = project_dir(project_id) / "dwg2pdf" / str(doc_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _doc_dxf_cache_dir(doc: Document) -> Path:
    """该 DWG 文档对应的 DXF 缓存目录（macOS 用户手动 ODA 转好后放在这里）."""
    cache_dir = DXF_CACHE_ROOT / f"doc_{doc.id}_{Path(doc.filename).stem}"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _find_cached_dxf(doc: Document) -> Path | None:
    """在该 DWG 文档对应的缓存目录中查找已转好的 DXF."""
    cache_dir = _doc_dxf_cache_dir(doc)
    candidates = sorted(cache_dir.glob("*.dxf"), key=lambda p: p.stat().st_mtime, reverse=True)
    for c in candidates:
        if c.stat().st_size > 0:
            return c
    return None


def _find_libreoffice() -> str | None:
    """查找 LibreOffice 可执行文件."""
    candidates = [
        "soffice", "libreoffice",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        "/usr/bin/soffice", "/usr/bin/libreoffice",
        "C:/Program Files/LibreOffice/program/soffice.exe",
    ]
    for c in candidates:
        if shutil.which(c) or Path(c).exists():
            return c
    return None


def _find_oda_cli() -> str | None:
    """查找 ODA File Converter CLI 可执行文件（Linux/Windows 服务器端用）.

    ODA 在 macOS 是 GUI 应用（必须用 cocoa Qt 平台），但 Linux/Windows 提供纯 CLI 版本，
    适合服务器 / Docker 自动转换。
    """
    env_bin = os.environ.get("ODA_CLI_BIN")
    if env_bin and (shutil.which(env_bin) or Path(env_bin).exists()):
        return env_bin

    candidates = [
        "ODAFileConverter",
        "/usr/bin/ODAFileConverter",
        "/usr/local/bin/ODAFileConverter",
        "/opt/ODAFileConverter/ODAFileConverter",
        str(Path.home() / "ODAFileConverter" / "ODAFileConverter"),
        "C:/Program Files/ODA/ODAFileConverter.exe",
        "C:/ODAFileConverter/ODAFileConverter.exe",
    ]
    for c in candidates:
        if shutil.which(c) or Path(c).exists():
            return c
    return None


def _convert_dwg_to_dxf_via_oda_cli(dwg_path: Path, dxf_path: Path,
                                     oda_cli: str) -> tuple[bool, str]:
    """用 ODA File Converter CLI 把 DWG 转为 DXF（仅 Linux/Windows 可用，macOS 上 ODA 是 GUI）。"""
    in_dir = Path(tempfile.mkdtemp())
    out_dir = Path(tempfile.mkdtemp())
    try:
        # ODA 接受目录输入；隔离目标 DWG 避免误转同目录其它文件
        staged = in_dir / dwg_path.name
        shutil.copy2(str(dwg_path), str(staged))
        # 参数：输入目录、输出目录、目标版本、目标类型、是否递归、是否 Audit
        r = subprocess.run(
            [oda_cli, str(in_dir), str(out_dir), "ACAD2018", "DXF", "0", "1"],
            capture_output=True, text=True, timeout=480,
        )
        dxf_files = sorted(out_dir.glob("*.dxf"))
        if not dxf_files:
            detail = (r.stderr or r.stdout or "").strip()
            return False, f"ODA CLI 转 DWG→DXF 失败：{detail[:200] or '未生成 DXF'}"
        shutil.copy2(str(dxf_files[0]), str(dxf_path))
        if dxf_path.exists() and dxf_path.stat().st_size > 0:
            return True, ""
        return False, "ODA CLI 未生成有效 DXF"
    except subprocess.TimeoutExpired:
        return False, "ODA CLI 转换超时（图纸过大，超过 8 分钟）"
    except Exception as e:
        return False, f"ODA CLI 异常：{e}"
    finally:
        shutil.rmtree(in_dir, ignore_errors=True)
        shutil.rmtree(out_dir, ignore_errors=True)


# ezdxf 渲染脚本（在独立子进程中执行）。
# 大图（上百 MB 的 DXF）渲染时内存可达数 GB，直接在 Streamlit 主进程里跑一旦被
# OOM killer 杀掉就是整个服务宕机；子进程 + RLIMIT_AS 把爆炸范围限制在子进程内，
# 失败后主进程还能回退 LibreOffice 链路。
#
# 真实工程图（尤其天正系列）的几个坑，下面逐一处理：
# 1. ezdxf 1.4+ 的 PyMuPdfBackend.get_pdf_bytes() 必须传 Page 对象；
# 2. modelspace 常有远端野实体/巨型空图框，把页面撑到几十米 → 用实体中心
#    密度网格定位主体内容区，render_box 聚焦渲染；
# 3. 中文标注是天正私有转义 \M+5XXXX（GBK 十六进制），ezdxf 不认识 → 预先解码；
#    ATTDEF/ATTRIB 的文本还可能藏在 tag 里；
# 4. 图纸文字样式引用本机不存在的字体（GOTHIC/SimSun/各种 SHX），
#    CJK 会渲染成空白 → 全部接管为 Noto Sans CJK SC（容器内提取自 TTC）；
# 5. HATCH 图案填充极易超时（每个 30s × 几十处）→ 只画轮廓 + 2s 超时熔断；
# 6. 视口指向失效区域（图纸画完又搬过位置）的图纸空间页面渲染出来接近空白
#    → 检测后跳过。
_EZDXF_RENDER_SCRIPT = r'''
import sys

try:
    import resource
    # 虚拟内存上限 4GB，超限后分配失败抛 MemoryError 而不是触发整机 OOM
    resource.setrlimit(resource.RLIMIT_AS, (4 * 1024**3, 4 * 1024**3))
except ImportError:
    pass  # Windows 无 resource 模块

dxf_path, pdf_path = sys.argv[1], sys.argv[2]

import os
import re
from pathlib import Path

import ezdxf
from ezdxf import bbox, units
from ezdxf.fonts import fonts
from ezdxf.addons.drawing import RenderContext, Frontend
from ezdxf.addons.drawing import layout as layout_mod
from ezdxf.addons.drawing.pymupdf import (
    PyMuPdfBackend, get_coordinate_output_space, Command)
from ezdxf.addons.drawing.recorder import (
    PointsRecord, SolidLinesRecord, PathRecord, FilledPathsRecord, ImageRecord)
from ezdxf.addons.drawing.config import (
    Configuration, HatchPolicy, BackgroundPolicy, ColorPolicy)
from ezdxf.addons.drawing.layout import Page, Margins, Units, Settings
from ezdxf.math import BoundingBox2d, Vec2

try:
    import pymupdf
except ImportError:
    import fitz as pymupdf


# ---------- 1. CJK 字体 ----------

def register_cjk_font():
    """返回可用的 CJK 字体文件名（注册进 ezdxf 字体缓存后），找不到返回 None."""
    fm = fonts.font_manager
    # Linux 容器：fonts-noto-cjk 只装 TTC，且缓存只记录 TTC 第一个 face（JP），
    # 提取 SC face 为独立 otf，保证中文以简体字形渲染
    extracted = Path("data") / "fonts" / "NotoSansCJKsc-Regular.otf"
    if not extracted.exists():
        ttc = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
        if ttc.exists():
            try:
                from fontTools.ttLib import TTCollection
                coll = TTCollection(str(ttc), lazy=True)
                for f in coll.fonts:
                    if "SC" in (f["name"].getDebugName(1) or ""):
                        extracted.parent.mkdir(parents=True, exist_ok=True)
                        f.save(str(extracted))
                        break
            except Exception:
                pass
    if extracted.exists():
        try:
            fm.scan_folder(extracted.parent)
        except Exception:
            pass
    try:
        fm.build()
    except Exception:
        pass
    if extracted.exists():
        # 直接用文件名；get_font_face 未命中时会静默回退 DejaVuSans，不能用它校验
        return extracted.name
    # macOS / 其它环境：按家族名找已安装的 CJK 字体
    for family in ("PingFang SC", "Hiragino Sans GB", "Songti SC",
                   "Microsoft YaHei", "SimSun", "Noto Sans CJK SC"):
        try:
            face = fm.find_font_face(family)
            if face and face.filename and face.family.lower().startswith(family.split()[0].lower()):
                return face.filename
        except Exception:
            continue
    return None


# ---------- 2. 天正 \M+5XXXX 转义解码 + 内联字体覆盖清理 ----------

_M5 = re.compile(r"\\M\+5([0-9A-Fa-f]{4})")
_MTEXT_FONT = re.compile(r"\\[fF][^;{}]*;")


def clean_text(s):
    if not s:
        return s
    if "\\M+5" in s:
        def rep(m):
            try:
                return bytes.fromhex(m.group(1)).decode("gbk")
            except Exception:
                return m.group(0)
        s = _M5.sub(rep, s)
    # MTEXT 内联字体覆盖（如 \fSimSun|b0|i0|c134|p2;）指向本机不存在的字体，
    # 会导致 CJK 缺字，剥掉后回落到文字样式字体（已接管为 CJK 字体）
    if "\\f" in s or "\\F" in s:
        s = _MTEXT_FONT.sub("", s)
    return s


def clean_doc_text(doc):
    def process(entities):
        for e in entities:
            t = e.dxftype()
            try:
                if t in ("TEXT", "ATTRIB", "ATTDEF"):
                    e.dxf.text = clean_text(e.dxf.text)
                    if t in ("ATTRIB", "ATTDEF"):
                        # 天正常把属性文字塞进 tag；渲染端在 text 为空时回落到 tag
                        tag = clean_text(getattr(e.dxf, "tag", "") or "")
                        if not (e.dxf.text or "").strip() and tag.strip():
                            e.dxf.text = tag
                        try:
                            e.dxf.tag = tag
                        except Exception:
                            pass
                elif t == "MTEXT":
                    e.text = clean_text(e.text)
                elif t == "DIMENSION":
                    e.dxf.text = clean_text(e.dxf.text or "")
                elif t in ("MLEADER", "MULTILEADER"):
                    mt = getattr(getattr(e, "context", None), "mtext", None)
                    if mt is not None and getattr(mt, "text", None):
                        mt.text = clean_text(mt.text)
            except Exception:
                continue

    process(doc.modelspace())
    for name in doc.layouts.names():
        if name != "Model":
            process(doc.layouts.get(name))
    for blk in doc.blocks:
        process(blk)


# ---------- 3. 主体内容区定位（密度网格） ----------

def dense_render_box(entities):
    """定位主体内容区 + 需要绘制的实体集合。

    返回 (region, keep_ids)：region 是渲染范围（drawing units），
    keep_ids 是包围盒与 region 相交的实体 id 集合（绘制时过滤野实体，
    避免无关实体撑大录制体积 —— get_pdf_bytes 的耗时与录制体积成正比，
    实测 2 万实体的图要 102 分钟）。

    region 的算法：真实图纸的 extents 常被野实体撑大到内容区的几十倍
    （实测 doc16：内容 1.4 万单位宽，extents 达 118 万），直接全量渲染
    内容会变成小点；但按"包围盒相交"并入又会被横穿主区的长 stray 线带偏。
    可靠做法：实体中心 1%-99% 分位框外扩 10% 作为主区，中心落在主区内的
    实体并入完整包围盒（探出边界的立面、引注不会被裁）；
    巨型实体（空图框、横跨长线）只并入主区附近 25% 范围的部分。
    """
    items = []  # (id, cx, cy, x0, y0, x1, y1)
    for e in entities:
        try:
            b = bbox.extents([e])
            if b.has_data:
                items.append((id(e),
                              (b.extmin.x + b.extmax.x) / 2,
                              (b.extmin.y + b.extmax.y) / 2,
                              b.extmin.x, b.extmin.y, b.extmax.x, b.extmax.y))
        except Exception:
            continue
    if len(items) < 5:
        return None, None
    xs = sorted(it[1] for it in items)
    ys = sorted(it[2] for it in items)
    n = len(items)
    px0, px1 = xs[int(n * .01)], xs[min(n - 1, int(n * .99))]
    py0, py1 = ys[int(n * .01)], ys[min(n - 1, int(n * .99))]
    pw, ph = px1 - px0, py1 - py0
    if pw <= 0 or ph <= 0:
        return None, None
    # 主区 = 分位框外扩 10%
    ex0, ex1 = px0 - pw * .1, px1 + pw * .1
    ey0, ey1 = py0 - ph * .1, py1 + ph * .1
    rx0, ry0, rx1, ry1 = ex0, ey0, ex1, ey1
    for _, cx, cy, x0, y0, x1, y1 in items:
        if not (ex0 <= cx <= ex1 and ey0 <= cy <= ey1):
            continue
        if (x1 - x0) > 3 * pw or (y1 - y0) > 3 * ph:
            # 巨型实体（空图框/横跨长线）：只并入主区附近 25% 的部分
            x0, y0 = max(x0, ex0 - pw * .25), max(y0, ey0 - ph * .25)
            x1, y1 = min(x1, ex1 + pw * .25), min(y1, ey1 + ph * .25)
            if x1 <= x0 or y1 <= y0:
                continue
        rx0, ry0 = min(rx0, x0), min(ry0, y0)
        rx1, ry1 = max(rx1, x1), max(ry1, y1)
    # 2% 边距
    mx, my = (rx1 - rx0) * .02, (ry1 - ry0) * .02
    region = (rx0 - mx, ry0 - my, rx1 + mx, ry1 + my)
    # 绘制过滤集：包围盒与 region 相交的实体保留，其余（远端野实体）不画
    keep_ids = set()
    gx0, gy0, gx1, gy1 = region
    for eid, _, _, x0, y0, x1, y1 in items:
        if x1 >= gx0 and x0 <= gx1 and y1 >= gy0 and y0 <= gy1:
            keep_ids.add(eid)
    return region, keep_ids


def page_for_region(region, doc_units, margin_mm=20):
    """按内容区宽高比生成 PDF 页面（上限 5000mm，超出时 fit_page 自动缩放）。"""
    factor = units.conversion_factor(doc_units, Units.mm)
    w_mm = (region[2] - region[0]) * factor
    h_mm = (region[3] - region[1]) * factor
    return Page(w_mm, h_mm, units=Units.mm, margins=Margins.all(margin_mm),
                max_width=5000, max_height=5000)


def fast_pdf_bytes(backend, page, settings, render_box, cfg):
    """把 PyMuPdfBackend 录制的图元直接写成 PDF 内容流，返回 PDF bytes。

    不能用 backend.get_pdf_bytes()：它经 pymupdf.Shape 逐条 finish，
    Shape 用 `self.draw_cont += ...` 字符串拼接（属性 += 无 in-place 优化），
    累计缓冲越大每条越慢，O(n²)——实测 13 万条图元要 102 分钟；
    直接拼内容流（list.append + join，O(n)）同样内容 90 秒。
    含图片记录时返回 None，调用方回退 get_pdf_bytes。
    """
    import copy as _copy
    MM = 72 / 25.4
    # 先检查图片记录：player.transform 会原地修改录制，
    # 若先变换再回退 get_pdf_bytes 会被二次变换
    for record in backend.records:
        if isinstance(record, ImageRecord):
            return None  # 含图片走慢速正确路径
    player = backend.player()
    if render_box is None:
        render_box = player.bbox()
    # 与 PyMuPdfBackend.get_replay 相同的页面布局/变换/裁剪流程
    output_layout = layout_mod.Layout(render_box, flip_y=True)
    page2 = output_layout.get_final_page(page, settings)
    settings2 = _copy.copy(settings)
    settings2.output_coordinate_space = get_coordinate_output_space(page2)
    m = output_layout.get_placement_matrix(page2, settings=settings2, top_origin=True)
    player.transform(m)
    if settings.crop_at_margins:
        p1, p2 = page2.get_margin_rect(top_origin=True)
        output_scale = settings2.page_output_scale_factor(page2)
        player.crop_rect(p1 * output_scale, p2 * output_scale, 0.1 * MM)

    W_pt = page2.width_in_mm * MM
    H_pt = page2.height_in_mm * MM
    doc_out = pymupdf.open()
    pg = doc_out.new_page(-1, max(1, int(W_pt)), max(1, int(H_pt)))
    ipctm = ~pg.transformation_matrix  # 输出空间(y向下) -> PDF(y向上)
    MA, MB, MC, MD, ME, MF = ipctm.a, ipctm.b, ipctm.c, ipctm.d, ipctm.e, ipctm.f

    parts = []
    ap = parts.append
    ap(f"1 1 1 rg\n0 0 {W_pt:.3f} {H_pt:.3f} re\nf\n")  # 白底

    def fmt(p):
        x = MA * p.x + MC * p.y + ME
        y = MB * p.x + MD * p.y + MF
        return f"{x:.3f} {y:.3f}"

    props_map = player.properties
    cur_stroke = [None]
    min_lw_mm = max(0.05, (cfg.min_lineweight or 0) * 25.4 / 300)
    lw_scaling = cfg.lineweight_scaling or 1.0

    def stroke_state(props):
        rgb = props.color
        r, g, b = int(rgb[1:3], 16) / 255, int(rgb[3:5], 16) / 255, int(rgb[5:7], 16) / 255
        w = max(0.1, max(min_lw_mm, props.lineweight) * MM * lw_scaling)
        key = (rgb[:7], round(w, 3))
        if key != cur_stroke[0]:
            ap(f"{r:.3f} {g:.3f} {b:.3f} RG\n{w:.3f} w\n1 J\n1 j\n")
            cur_stroke[0] = key

    def fill_state(props):
        rgb = props.color
        r, g, b = int(rgb[1:3], 16) / 255, int(rgb[3:5], 16) / 255, int(rgb[5:7], 16) / 255
        ap(f"{r:.3f} {g:.3f} {b:.3f} rg\n")

    def emit_path(path, close):
        start = path.start
        sub_start = start
        last = start
        for cmd in path.commands():
            end = cmd.end
            if cmd.type == Command.MOVE_TO:
                if close and not sub_start.isclose(last):
                    ap("h\n")
                ap(f"{fmt(end)} m\n")
                sub_start = end
            elif cmd.type == Command.LINE_TO:
                ap(f"{fmt(end)} l\n")
            elif cmd.type == Command.CURVE3_TO:
                c = cmd.ctrl
                c1 = start + (c - start) * (2 / 3)
                c2 = end + (c - end) * (2 / 3)
                ap(f"{fmt(c1)} {fmt(c2)} {fmt(end)} c\n")
            elif cmd.type == Command.CURVE4_TO:
                ap(f"{fmt(cmd.ctrl1)} {fmt(cmd.ctrl2)} {fmt(end)} c\n")
            start = end
            last = end
        if close and not sub_start.isclose(last):
            ap("h\n")

    for record in player.records:
        props = props_map[record.property_hash]
        if isinstance(record, PointsRecord):
            n = len(record.points)
            if n == 0:
                continue
            vs = record.points.vertices()
            if n > 2:
                fill_state(props)
                ap(f"{fmt(vs[0])} m\n")
                for v in vs[1:]:
                    ap(f"{fmt(v)} l\n")
                ap("h\nf*\n")
            else:
                stroke_state(props)
                if len(vs) == 1:
                    ap(f"{fmt(vs[0])} m\n{fmt(vs[0])} l\nS\n")
                else:
                    ap(f"{fmt(vs[0])} m\n{fmt(vs[1])} l\nS\n")
        elif isinstance(record, SolidLinesRecord):
            stroke_state(props)
            vs = record.lines.vertices()
            for i in range(0, len(vs) - 1, 2):
                ap(f"{fmt(vs[i])} m\n{fmt(vs[i + 1])} l\n")
            ap("S\n")
        elif isinstance(record, PathRecord):
            if len(record.path) == 0:
                continue
            stroke_state(props)
            emit_path(record.path, close=False)
            ap("S\n")
        elif isinstance(record, FilledPathsRecord):
            fill_state(props)
            for pth in record.paths:
                emit_path(pth, close=True)
            ap("f*\n")

    stream = "".join(parts).encode()
    xref = pymupdf.TOOLS._insert_contents(pg, b" ", 1)
    doc_out.update_stream(xref, stream)
    return doc_out.tobytes()


def layout_is_stale(layout, msp_region):
    """图纸空间的视口全部看不到 modelspace 主内容区 → 该页渲染出来接近空白。"""
    if msp_region is None:
        return False
    vps = []
    for e in layout:
        if e.dxftype() != "VIEWPORT":
            continue
        try:
            if e.dxf.status > 0 and e.dxf.id != 1:  # id=1 是图纸空间自身的视口
                vps.append(e)
        except Exception:
            continue
    if not vps:
        return False
    x0, y0, x1, y1 = msp_region
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    hx, hy = (x1 - x0) / 2, (y1 - y0) / 2
    for vp in vps:
        try:
            vc = vp.dxf.view_center_point
            vh = vp.dxf.view_height or 0
            if abs(vc.x - cx) <= hx + vh and abs(vc.y - cy) <= hy + vh:
                return False  # 有视口能看到内容
        except Exception:
            return False
    return True


# ---------- 主流程 ----------

import time
import tempfile

font_file = register_cjk_font()

doc = ezdxf.readfile(dxf_path)
doc_units = Units(doc.units)

if font_file:
    # 全部文字样式接管为 CJK 字体（原样式引用的 GOTHIC/SimSun/SHX 本机都没有）
    for st in doc.styles:
        try:
            st.dxf.font = font_file
            st.dxf.bigfont = ""
        except Exception:
            pass

clean_doc_text(doc)

cfg = Configuration(
    background_policy=BackgroundPolicy.WHITE,
    color_policy=ColorPolicy.BLACK,       # 黑白出图（同 AutoCAD monochrome.ctb）
    hatch_policy=HatchPolicy.SHOW_OUTLINE,  # 图案填充极易超时，只画轮廓
    hatching_timeout=2.0,
)

msp = doc.modelspace()
msp_region, msp_keep_ids = dense_render_box(msp)

layout_names = ["Model"]
for name in sorted(doc.layouts.names()):
    if name == "Model":
        continue
    layout = doc.layouts.get(name)
    # 只要有任何图元就保留 —— 老版本的"视口看不到主区就跳过"启发式把
    # 立面/详图（视口中心在卧室/厨房，不在 msp 中心）误判为 stale，
    # 导致最终 PDF 只剩 1 页。用户明确要求 PDF 包含所有 sheet，宁可
    # 多渲一张也不漏。
    if not any(True for _ in layout):
        continue
    layout_names.append(name)


def render_layout_pdf(layout_name):
    """渲染单个 layout，返回 PDF bytes（失败返回 None）。

    在 fork 出的子进程里跑时，doc/cfg/doc_units/msp_region 全部从父进程
    写时复制继承，不需要重读上百 MB 的 DXF。
    """
    try:
        layout = doc.modelspace() if layout_name == "Model" else doc.layouts.get(layout_name)
        ctx = RenderContext(doc)
        backend = PyMuPdfBackend()
        fe = Frontend(ctx, backend, config=cfg)
        if layout_name == "Model" and msp_keep_ids:
            # 只画内容区内的实体：野实体不录制，get_pdf_bytes 体积/耗时大降
            fe.draw_layout(layout, finalize=True,
                           filter_func=lambda e: id(e) in msp_keep_ids)
        elif layout_name != "Model":
            # 图纸空间过滤 VIEWPORT：每张图常带几十个视口，ezdxf 会把
            # modelspace 全量 1-2 万图元展开进每个视口，单页卡 10+ 分钟。
            # 视口只是裁剪框，剥掉后其余图元（边线/文字/块引用）照常呈现。
            fe.draw_layout(layout, finalize=True,
                           filter_func=lambda e: e.dxftype() != 'VIEWPORT')
        else:
            fe.draw_layout(layout, finalize=True)
        settings = Settings(fit_page=True)
        if layout_name == "Model":
            region = msp_region
            if region is None:
                ext = bbox.extents(layout)
                if not ext.has_data:
                    return None
                region = (ext.extmin.x, ext.extmin.y, ext.extmax.x, ext.extmax.y)
            page = page_for_region(region, doc_units)
            rbox = BoundingBox2d([Vec2(region[0], region[1]), Vec2(region[2], region[3])])
        else:
            page = Page.from_dxf_layout(layout)
            if page.width_in_mm <= 0 or page.height_in_mm <= 0:
                ext = bbox.extents(layout)
                if not ext.has_data:
                    return None
                region = (ext.extmin.x, ext.extmin.y, ext.extmax.x, ext.extmax.y)
                page = page_for_region(region, doc_units)
            rbox = None
        data = fast_pdf_bytes(backend, page, settings, rbox, cfg)
        if data is not None:
            return data
        # 含图片记录：回退官方慢速路径
        return backend.get_pdf_bytes(page, settings=settings, render_box=rbox)
    except Exception as e:
        print(f"layout {layout_name} 渲染失败: {e}", file=sys.stderr, flush=True)
        return None


def child_render(layout_name, out_file):
    data = render_layout_pdf(layout_name)
    if data:
        Path(out_file).write_bytes(data)


# 图纸空间多的大图（10+ 个 layout，每个都要重画 modelspace 内容）串行要几个小时，
# 用 fork 子进程并行（3 并发），每个 layout 有独立时间预算，超时/崩溃只丢那一页。
BUDGET = {"Model": 1800}     # Model 页是主内容，预算放宽
BUDGET_PAPER = 600           # 每个图纸空间页预算
WORKERS = 3

results = {}   # name -> pdf bytes
timed_out = []

try:
    import multiprocessing as mp
    if "fork" in mp.get_all_start_methods():
        mp.set_start_method("fork")
        use_parallel = True
    else:
        use_parallel = False
except Exception:
    use_parallel = False

tmp_dir = Path(tempfile.mkdtemp(prefix="ezdxf_pages_"))

if use_parallel and len(layout_names) > 1:
    pending = list(layout_names)
    while pending:
        batch, pending = pending[:WORKERS], pending[WORKERS:]
        procs = {}
        for nm in batch:
            out_f = tmp_dir / f"{layout_names.index(nm)}.pdf"
            p = mp.Process(target=child_render, args=(nm, str(out_f)))
            p.start()
            procs[nm] = (p, out_f, time.time())
        for nm, (p, out_f, t0) in procs.items():
            budget = BUDGET.get(nm, BUDGET_PAPER)
            p.join(max(1.0, budget - (time.time() - t0)))
            if p.is_alive():
                p.terminate()
                p.join()
                timed_out.append(nm)
                print(f"layout {nm} 超过 {budget}s 预算，跳过", file=sys.stderr, flush=True)
                continue
            if out_f.exists() and out_f.stat().st_size > 0:
                results[nm] = out_f.read_bytes()
                print(f"layout {nm}: {time.time()-t0:.0f}s", file=sys.stderr, flush=True)
else:
    for nm in layout_names:
        t0 = time.time()
        data = render_layout_pdf(nm)
        if data:
            results[nm] = data
            print(f"layout {nm}: {time.time()-t0:.0f}s", file=sys.stderr, flush=True)

if not results:
    print("所有 layout 渲染失败", file=sys.stderr)
    sys.exit(4)

pdf_doc = pymupdf.open()
for nm in layout_names:
    if nm not in results:
        continue
    sub = pymupdf.open(stream=results[nm], filetype="pdf")
    pdf_doc.insert_pdf(sub)
    sub.close()
pdf_doc.save(pdf_path)
pdf_doc.close()

note = f"已渲染 {len(results)}/{len(layout_names)} 个页面"
if timed_out:
    note += f"，{len(timed_out)} 页超时跳过"
print(note)
'''


def _dxf_to_pdf_via_ezdxf(dxf_path: Path, pdf_path: Path) -> tuple[bool, str]:
    """用 ezdxf + pymupdf 后端把 DXF 的所有 layout 渲染为多页 PDF（保留每张图纸的真实尺寸）。

    渲染在独立子进程中进行（内存上限 4GB / 超时 90 分钟），失败可安全回退。
    百 MB 级的真实工程图 readfile 就要几分钟，图纸空间再多就按 layout 数倍增；
    子进程内部按 layout 并行渲染（fork 共享文档 + 每页独立时间预算），
    单页超时只丢那一页，不会整份回退 LibreOffice 压成 A4 废纸。
    """
    if not EZDXF_AVAILABLE:
        return False, "未安装 ezdxf，无法渲染 DXF。"

    try:
        r = subprocess.run(
            [sys.executable, "-c", _EZDXF_RENDER_SCRIPT, str(dxf_path), str(pdf_path)],
            capture_output=True, text=True, timeout=5400,
        )
    except subprocess.TimeoutExpired:
        return False, "ezdxf 渲染超时（图纸过大，超过 90 分钟）"
    except Exception as e:
        return False, f"ezdxf 渲染进程异常：{e}"

    if r.returncode == 0 and pdf_path.exists() and pdf_path.stat().st_size > 0:
        return True, (r.stdout or "").strip()

    detail = (r.stderr or r.stdout or "").strip().splitlines()
    tail = detail[-1][:200] if detail else f"exit={r.returncode}"
    return False, f"ezdxf 渲染失败：{tail}"


def _dxf_to_pdf_via_libreoffice(dxf_path: Path, pdf_path: Path) -> tuple[bool, str]:
    """用 LibreOffice 把 DXF 转 PDF（备份方案；layout 会被压成 A4）。"""
    lo = _find_libreoffice()
    if not lo:
        return False, "未找到 LibreOffice"
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        r = subprocess.run(
            [lo, "--headless", "--convert-to", "pdf",
             "--outdir", str(tmp_dir), str(dxf_path)],
            capture_output=True, text=True, timeout=300,
        )
        pdf_files = sorted(tmp_dir.glob("*.pdf"))
        if not pdf_files:
            return False, f"LibreOffice 转 DXF→PDF 失败：{(r.stderr or r.stdout or '')[:200]}"
        shutil.copy2(str(pdf_files[0]), str(pdf_path))
        if pdf_path.exists() and pdf_path.stat().st_size > 0:
            return True, ""
        return False, "LibreOffice 未生成有效 PDF"
    except subprocess.TimeoutExpired:
        return False, "LibreOffice 转换超时"
    except Exception as e:
        return False, f"LibreOffice 异常：{e}"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _convert_dwg_to_pdf(dwg_path: Path, project_id: int, doc: Document) -> tuple[Path | None, str | None]:
    """把 DWG 转为 PDF。

    平台自适应策略：
      Linux/Windows（服务器端）：
        1. Jenkins dwg2pdf job（配置了环境变量时优先，GUI 质量最好）
        2. ODA File Converter CLI：DWG → DXF → ezdxf 渲染多页 PDF
        3. 失败时回退 LibreOffice：DXF → PDF
      macOS（ODA 是 GUI 无法 CLI）：
        1. 优先用缓存目录里用户已用 ODA GUI 转好的 DXF
        2. ezdxf / LibreOffice 渲染为 PDF
        3. 兜底：LibreOffice 直接 DWG → PDF（仅旧版 DWG 有效）

    命中 PDF 缓存时直接返回。
    """
    out_dir = _dwg2pdf_dir(project_id, doc.id)
    pdf_path = out_dir / f"{dwg_path.stem}.pdf"

    if pdf_path.exists() and pdf_path.stat().st_size > 0:
        return pdf_path, None

    oda_cli = _find_oda_cli()
    last_err = ""

    # ============ Linux/Windows 自动路径 ============
    if not _is_macos():
        # 1. Jenkins dwg2pdf job（配置了就走，质量最接近 CAD 原生效果）
        if _jenkins_available():
            ok, msg = _convert_dwg_to_pdf_via_jenkins(dwg_path, pdf_path)
            if ok and pdf_path.exists() and pdf_path.stat().st_size > 0:
                return pdf_path, None
            last_err += f"Jenkins：{msg or '转换失败'}；"

        # 2. ODA CLI + ezdxf（次选，本地一条龙不依赖外部服务）
        if oda_cli:
            dxf_path = out_dir / f"{dwg_path.stem}.dxf"
            if not dxf_path.exists() or dxf_path.stat().st_size == 0:
                ok, err = _convert_dwg_to_dxf_via_oda_cli(dwg_path, dxf_path, oda_cli)
                if not ok:
                    last_err += f"ODA CLI：{err}"

            if dxf_path.exists() and dxf_path.stat().st_size > 0:
                ok, msg = _dxf_to_pdf_via_ezdxf(dxf_path, pdf_path)
                if ok:
                    return pdf_path, None
                last_err += f"；ezdxf：{msg}"
                ok2, msg2 = _dxf_to_pdf_via_libreoffice(dxf_path, pdf_path)
                if ok2:
                    return pdf_path, None
                last_err += f"；LO：{msg2}"
                return None, last_err or "Linux 自动转换失败"

    # ============ macOS / 兜底路径 ============
    # 1. 优先用缓存的 DXF（macOS 用户用 ODA GUI 手动转的）
    cached_dxf = _find_cached_dxf(doc)
    if cached_dxf:
        ok, msg = _dxf_to_pdf_via_ezdxf(cached_dxf, pdf_path)
        if ok:
            return pdf_path, None
        ok2, msg2 = _dxf_to_pdf_via_libreoffice(cached_dxf, pdf_path)
        if ok2:
            return pdf_path, None
        return None, f"缓存 DXF 渲染失败：{msg}；{msg2}"

    # 2. 兜底：LibreOffice 直接 DWG → PDF（仅旧版 DWG 有效）
    lo = _find_libreoffice()
    if lo:
        tmp_dir = Path(tempfile.mkdtemp())
        try:
            r = subprocess.run(
                [lo, "--headless", "--convert-to", "pdf",
                 "--outdir", str(tmp_dir), str(dwg_path)],
                capture_output=True, text=True, timeout=300,
            )
            pdf_files = sorted(tmp_dir.glob("*.pdf"))
            if pdf_files:
                shutil.copy2(str(pdf_files[0]), str(pdf_path))
                if pdf_path.exists() and pdf_path.stat().st_size > 0:
                    return pdf_path, None
            last_err += f"；LO 直转：{(r.stderr or r.stdout or '')[:200]}"
        except Exception as e:
            last_err += f"；LO 异常：{e}"
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    if last_err:
        return None, f"DWG 转 PDF 失败：{last_err}"

    return None, (
        "DWG 转 PDF 失败：未找到可用的转换路径。\n\n"
        + ("macOS 用户：请用 ODA File Converter（GUI）把 DWG 转成 DXF 中间文件，"
           "放到页面显示的缓存目录里，刷新页面即可。"
           if _is_macos() else
           "Linux/Windows 用户：可配置 JENKINS_* 环境变量走 Jenkins dwg2pdf job，"
           "或安装 ODA File Converter CLI 并设置 ODA_CLI_BIN，或安装 LibreOffice。")
    )


def _save_uploaded_dwg(project_id: int, uploaded_file) -> int:
    """保存上传的 DWG 文件，并写入 Document 记录，返回 doc_id."""
    target_dir = documents_dir(project_id, "03_drawings")
    dest = target_dir / uploaded_file.name
    if dest.exists():
        stem = dest.stem
        suffix = dest.suffix
        timestamp = beijing_timestamp()
        dest = target_dir / f"{stem}_{timestamp}{suffix}"
    dest.write_bytes(uploaded_file.getbuffer())

    with session() as s:
        doc = Document(
            project_id=project_id,
            category="03_drawings",
            filename=dest.name,
            path=str(dest.relative_to(project_dir(project_id))),
            size_bytes=dest.stat().st_size,
            uploaded_by_id=_user_id(),
        )
        s.add(doc)
        s.flush()
        doc_id = doc.id
        s.commit()
        return doc_id


def _convert_dxf_to_svg(dxf_path: Path, output_dir: Path) -> tuple[list[Path], str | None]:
    """直接从 DXF 渲染每 layout 一张 SVG（黑白，文字可编辑）。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(output_dir.glob("sheet_*.svg"))
    if existing:
        return existing, None

    import textwrap
    import subprocess

    SCRIPT = textwrap.dedent(r"""
        import sys
        from pathlib import Path
        import ezdxf
        from ezdxf.addons.drawing import Frontend, RenderContext, svg
        from ezdxf.addons.drawing.properties import LayoutProperties
        from ezdxf.addons.drawing.config import Configuration, LinePolicy, HatchPolicy, TextPolicy, ColorPolicy
        from ezdxf.addons.drawing.layout import Page
        from ezdxf.bbox import extents
        import matplotlib
        import matplotlib.font_manager as fm
        zh = [f.name for f in fm.fontManager.ttflist if 'CJK' in f.name and 'SC' in f.name]
        if zh:
            matplotlib.rcParams['font.sans-serif'] = zh + ['DejaVu Sans']
            matplotlib.rcParams['axes.unicode_minus'] = False
        dxf_path, layout_name, out_path = sys.argv[1:4]
        doc = ezdxf.readfile(dxf_path)
        cfg = Configuration(
            hatch_policy=HatchPolicy.IGNORE,
            line_policy=LinePolicy.APPROXIMATE,
            text_policy=TextPolicy.FILLING,
            color_policy=ColorPolicy.BLACK,
            # 这里的 min_lineweight 单位是 1/300 inch：
            #   0.5 → 0.5/300 inch ≈ 0.04mm
            #   1.0 → 1.0/300 inch ≈ 0.085mm
            # ABSOLUTE 策略下：最终线宽 = max(min_lineweight, 实体线宽) * lineweight_scaling
            # 0.25mm 默认线宽 × 0.1 = 0.025mm（比 0.1mm 文字还细，符合"细线"观感）
            lineweight_scaling=0.1,
            min_lineweight=0.5,
        )
        layout = doc.modelspace() if layout_name == 'Model' else doc.layouts.get(layout_name)
        if layout is None or not any(True for _ in layout):
            sys.exit(0)
        b = svg.SVGBackend()
        fp = Frontend(RenderContext(doc), b, cfg)
        lp = LayoutProperties.from_layout(layout)
        # 过滤 VIEWPORT：paperspace 里每张图常带几十个 VIEWPORT，
        # ezdxf 会把 Model 全量 1-2 万图元展开进每个视口，单张卡 10+ 分钟。
        # 视口本身只是裁剪框，剥掉后其余图元（边线/文字/块引用）仍能正确呈现。
        fp.draw_layout(layout, layout_properties=lp,
                       filter_func=lambda e: e.dxftype() != 'VIEWPORT')
        try:
            page = Page.from_dxf_layout(layout)
        except Exception:
            ext = extents(layout)
            if not ext.has_data:
                sys.exit(0)
            page = Page(width=ext.extmax.x - ext.extmin.x, height=ext.extmax.y - ext.extmin.y)
        s = b.get_string(page)
        Path(out_path).write_text(s, encoding='utf-8')
    """)

    try:
        doc = ezdxf.readfile(str(dxf_path))
    except Exception as e:
        return [], f"无法读取 DXF：{e}"

    layout_names: list[str] = []
    msp = doc.modelspace()
    if any(True for _ in msp):
        layout_names.append("Model")
    for nm in sorted(doc.layouts.names()):
        if nm == "Model":
            continue
        layout = doc.layouts.get(nm)
        if not any(True for _ in layout):
            continue
        layout_names.append(nm)

    if not layout_names:
        return [], "DXF 中没有可渲染的 layout。"

    script_path = Path(tempfile.mktemp(suffix=".py", prefix="dxf2svg_"))
    script_path.write_text(SCRIPT, encoding="utf-8")

    WORKERS = 3
    BUDGET = 600
    pending = list(layout_names)
    while pending:
        batch, pending = pending[:WORKERS], pending[WORKERS:]
        procs: list = []
        for nm in batch:
            safe = nm.replace("/", "_").replace(" ", "_").replace(",", "_")
            out = output_dir / f"sheet_{safe}.svg"
            if out.exists():
                out.unlink()
            p = subprocess.Popen(
                [sys.executable, str(script_path), str(dxf_path), nm, str(out)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            procs.append((nm, p, out))
        for _nm, p, _out in procs:
            try:
                p.wait(BUDGET)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(5)
    script_path.unlink(missing_ok=True)

    saved = sorted(p for p in output_dir.glob("sheet_*.svg") if p.stat().st_size > 0)
    if not saved:
        return [], "所有 layout 渲染都失败了。"
    return saved, None


def _convert_pdf_to_svg(pdf_path: Path, output_dir: Path,
                        decimals: int = 2) -> tuple[list[Path], str | None]:
    """（备用）把 PDF 每页转为黑白 SVG—— 当 DXF 不在时兜底。"""
    """把 PDF 每页转为黑白 SVG（每页一张）—— 用于矢量下载。

    PyMuPDF 的 get_svg_image 会把每条图元展开成独立 path，13 万图元会出来
    100MB+。这里做两个减负：
    - 坐标精度从 6 位小数降到 2 位，path 数据通常减半
    - 注入 <style> 强制所有 fill/stroke 为黑；保持背景白
    同时把 Noto Sans CJK SC 写进 font-family，外部查看器（Figma/Inkscape/
    浏览器）渲染新增/可编辑文本时优先用中文字体（PDF 内文字已被渲染成 path，
    故已存在的字符无法换字体，但样式仍可保证外部新增文本美观）。
    """
    import re
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(output_dir.glob("sheet_*.svg"))
    if existing:
        return existing, None
    if not PYMUPDF_AVAILABLE:
        return [], "缺少 PyMuPDF，无法导出 SVG。"

    try:
        pdf_doc = fitz.open(str(pdf_path))
    except Exception as e:
        return [], f"无法打开 PDF：{e}"

    bw_style = (
        '<style>'
        '* { fill: #000 !important; stroke: #000 !important; }'
        'text { font-family: "Noto Sans CJK SC","Noto Sans CJK","Source Han Sans SC",'
        '"Microsoft YaHei","SimHei",sans-serif; }'
        '</style>'
    )
    coord_re = re.compile(r'-?\d+\.\d{3,}')

    def round_coords(s: str) -> str:
        def repl(m: re.Match) -> str:
            n = float(m.group(0))
            return f"{n:.{decimals}f}"
        return coord_re.sub(repl, s)

    saved: list[Path] = []
    try:
        for page_index in range(pdf_doc.page_count):
            page = pdf_doc.load_page(page_index)
            raw = page.get_svg_image(text_as_path=0)
            # 减负 + 注入 B&W 样式
            compressed = round_coords(raw)
            bw = re.sub(r'(<svg[^>]*>)', r'\1' + bw_style, compressed, count=1)
            name = f"sheet_p{page_index + 1}.svg"
            (output_dir / name).write_text(bw, encoding="utf-8")
            saved.append(output_dir / name)
    except Exception as e:
        return [], f"生成 SVG 失败：{e}"
    finally:
        pdf_doc.close()
    return saved, None


def _generate_panzoom_previews(pdf_path: Path, project_id: int, doc: Document,
                                target_long_px: int = 5000) -> tuple[list[Path], str | None]:
    """为网页 pan/zoom 预览生成中等分辨率的 PNG（比 pages/ 小 ~2.4 倍，
    保证 base64 内联到 HTML 时单页 < 5MB；用户在前端任意缩放仍能看清细节）。

    与 _convert_pdf_to_images 的区别：后者为了"放大到正常尺寸看清"做到 12000px
    长边，单页 30-50MB PNG 只能展示不能交互；本函数追求 5000px 长边 + pan/zoom
    让用户主动缩放，体验好且加载快。
    """
    output_dir = _dwg2pdf_dir(project_id, doc.id) / "previews"
    output_dir.mkdir(parents=True, exist_ok=True)

    existing = sorted(output_dir.glob("preview_*.png"))
    if existing:
        return existing, None

    if not PYMUPDF_AVAILABLE:
        return [], "缺少 PDF 渲染依赖：请安装 PyMuPDF。"

    try:
        pdf_doc = fitz.open(str(pdf_path))
    except Exception as e:
        return [], f"无法打开 PDF：{e}"

    saved: list[Path] = []
    try:
        for page_index in range(pdf_doc.page_count):
            page = pdf_doc.load_page(page_index)
            long_pt = max(page.rect.width, page.rect.height, 1.0)
            dpi = max(1, int(target_long_px * 72 / long_pt))
            out = page.get_pixmap(dpi=dpi, alpha=False)
            name = f"preview_p{page_index + 1}.png"
            out.save(str(output_dir / name))
            saved.append(output_dir / name)
    except Exception as e:
        return [], f"生成预览失败：{e}"
    finally:
        pdf_doc.close()
    return saved, None


def _convert_pdf_to_images(pdf_path: Path, project_id: int, doc: Document) -> tuple[list[Path], str | None]:
    """将 PDF 用 PyMuPDF 拆为逐页 PNG。命中缓存时直接返回已有图片。"""
    output_dir = _dwg2pdf_dir(project_id, doc.id) / "pages"
    output_dir.mkdir(parents=True, exist_ok=True)

    existing = sorted(output_dir.glob("page_*.png"))
    if existing:
        return existing, None

    if not PYMUPDF_AVAILABLE:
        return [], "缺少 PDF 渲染依赖：请安装 PyMuPDF（pip install PyMuPDF）。"

    try:
        pdf_doc = fitz.open(str(pdf_path))
    except Exception as e:
        return [], f"无法打开转换后的 PDF：{str(e)}"

    try:
        for page_index in range(pdf_doc.page_count):
            page = pdf_doc.load_page(page_index)
            # 自适应 dpi：ezdxf 渲染的工程图页面可能有几米宽（PDF 上限 ~5000mm），
            # 固定 150dpi 会产出几万像素的 PNG。按最长边 ~12000px 折算 dpi
            # （2400px 时 3mm 高的标注文字只有几个像素，放大后完全看不清），
            # 普通 A 系列页面不受影响（折算值 > 150 仍按 150 出图）。
            max_side_pt = max(page.rect.width, page.rect.height, 1.0)
            dpi = int(min(150, 12000 * 72 / max_side_pt))
            dpi = max(36, dpi)
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            pix.save(str(output_dir / f"page_{page_index + 1:03d}.png"))
    except Exception as e:
        return [], f"PDF 拆页为 PNG 失败：{str(e)}"
    finally:
        pdf_doc.close()

    pages = sorted(output_dir.glob("page_*.png"))
    if not pages:
        return [], "PDF 拆页后未生成图片。"
    return pages, None


def _crop_is_blank_or_solid(pix) -> bool:
    """判断一个 Pixmap 是否为"空白"或"单一颜色"。

    - 空白：墨迹像素占比 < 0.1%
    - 单一颜色：非白像素里同一灰度值占比 >= 70%（纯色填充块——
      内部均匀；文字/线条图的笔画边缘有大量不同灰度，< 70%）

    抽样分析以提速（采样到 ~20 万像素足够稳定）。
    """
    import numpy as np
    g = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width,
                                                          pix.n)[:, :, 0]
    step = max(1, int((g.size / 200000) ** 0.5))
    s = g[::step, ::step]
    ink_mask = s < 250
    if float(ink_mask.mean()) < 0.001:
        return True
    ink_vals = s[ink_mask]
    if ink_vals.size == 0:
        return True
    vals = np.bincount(ink_vals, minlength=256)
    if vals.max() / ink_vals.size > 0.7:
        return True
    return False


def _content_boxes(ink, merge_mm: float, dpi: int, pool: int = 4,
                   min_ink: int = 20) -> list[tuple[int, int, int, int]]:
    """连通域内容切分：返回墨迹内容框列表 [(x0, y0, x1, y1)]（原图坐标）。

    先 pool×pool 最大池化降采样（提速 ~16 倍），再按 merge_mm 半径膨胀墨迹——
    同一幅图样内部空隙（尺寸标注与图形之间）被桥接成一个连通域，
    不同图样之间的宽空白带保持分离；逐连通域取原始墨迹的包围盒。

    merge_mm 越大、相距更远的内容越倾向于合并为同一个矩形；
    60mm 适合普通施工图：把同一幅图样附近的文字标注/尺寸并入主图。
    """
    import numpy as np
    from scipy import ndimage

    h, w = ink.shape
    h4, w4 = h // pool * pool, w // pool * pool
    small = ink[:h4, :w4].reshape(h4 // pool, pool, w4 // pool, pool).max(axis=(1, 3))
    r = max(1, int(merge_mm * (dpi // pool) / 25.4))
    dilated = ndimage.binary_dilation(small, structure=np.ones((r, r)))
    labels, _count = ndimage.label(dilated)
    boxes: list[tuple[int, int, int, int]] = []
    for sl in ndimage.find_objects(labels):
        if sl is None:
            continue
        ys, xs = sl
        sub = small[ys, xs]
        if int(sub.sum()) < min_ink:
            continue  # 碎屑
        rows = np.where(sub.any(axis=1))[0]
        cols = np.where(sub.any(axis=0))[0]
        boxes.append((int((xs.start + cols[0]) * pool), int((ys.start + rows[0]) * pool),
                      int((xs.start + cols[-1] + 1) * pool), int((ys.start + rows[-1] + 1) * pool)))
    return boxes


def _split_pdf_to_content_images(pdf_path: Path, project_id: int, doc: Document,
                                 merge_mm: int = 30) -> tuple[list[Path], str | None]:
    """把每页 PDF 按内容自动切分：墨迹连通的合并为一个长方形，空白剔除。

    流程：72dpi 灰度栅格化 → 二值墨迹图 → pool 降采样 → 按 merge_mm
    膨胀墨迹 → 连通域标记 → 每个连通域取原墨迹包围盒 → 从 PDF 矢量按
    150dpi 重新裁剪渲染（文字保持清晰）→ 空白/单色块剔除。

    命名：auto_p{N}_{NNN}.png（与旧 crop_*/grid_* 区分，方便混合清理）。
    """
    output_dir = _dwg2pdf_dir(project_id, doc.id) / "crops"
    output_dir.mkdir(parents=True, exist_ok=True)

    existing = sorted(output_dir.glob("auto_*.png"))
    if existing:
        return existing, None

    # 旧格式清掉：连通域的 crop_*、网格的 grid_*
    for legacy in list(output_dir.glob("crop_*.png")) + list(output_dir.glob("grid_*.png")):
        legacy.unlink()

    if not PYMUPDF_AVAILABLE:
        return [], "缺少 PDF 渲染依赖：请安装 PyMuPDF（pip install PyMuPDF）。"

    try:
        import numpy as np
    except ImportError:
        return [], "缺少 numpy，无法做内容切分。"

    ANALYSIS_DPI = 72
    OUT_DPI = 150
    MARGIN_MM = 8
    MIN_BOX_PX = 300  # 原图坐标的最小边长 < 300px 视为碎片丢弃

    try:
        pdf_doc = fitz.open(str(pdf_path))
    except Exception as e:
        return [], f"无法打开转换后的 PDF：{str(e)}"

    scale = ANALYSIS_DPI / 72
    margin_px = int(MARGIN_MM * ANALYSIS_DPI / 25.4)

    staging_dir = Path(tempfile.mkdtemp(prefix="auto_"))
    saved: list[Path] = []
    try:
        for page_index in range(pdf_doc.page_count):
            page = pdf_doc.load_page(page_index)
            pix = page.get_pixmap(dpi=ANALYSIS_DPI, colorspace=fitz.csGRAY, alpha=False)
            gray = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
            ink = gray < 254
            boxes = _content_boxes(ink, merge_mm, ANALYSIS_DPI)
            kept = [(max(0, x0 - margin_px), max(0, y0 - margin_px),
                     min(pix.width, x1 + margin_px), min(pix.height, y1 + margin_px))
                    for (x0, y0, x1, y1) in boxes
                    if (x1 - x0) >= MIN_BOX_PX and (y1 - y0) >= MIN_BOX_PX]
            # 阅读顺序：行带优先（每 400px 一带），带内按 x 排序
            kept.sort(key=lambda b: (round(b[1] / 400), b[0]))
            for crop_index, (x0, y0, x1, y1) in enumerate(kept):
                clip = fitz.Rect(x0 / scale, y0 / scale, x1 / scale, y1 / scale)
                long_pt = max(clip.width, clip.height, 1.0)
                out_dpi = max(36, int(min(OUT_DPI, 12000 * 72 / long_pt)))
                try:
                    out = page.get_pixmap(dpi=out_dpi, clip=clip, alpha=False)
                except Exception:
                    try:
                        out = page.get_pixmap(dpi=36, clip=clip, alpha=False)
                    except Exception:
                        continue
                if _crop_is_blank_or_solid(out):
                    continue
                name = f"auto_p{page_index + 1}_{crop_index + 1:03d}.png"
                out.save(str(staging_dir / name))
                saved.append(staging_dir / name)
    except Exception as e:
        shutil.rmtree(staging_dir, ignore_errors=True)
        return [], f"按内容切分图片失败：{str(e)}"
    finally:
        pdf_doc.close()

    if not saved:
        shutil.rmtree(staging_dir, ignore_errors=True)
        return [], "切分后未得到有效图片。"
    for f in saved:
        shutil.move(str(f), output_dir / f.name)
    shutil.rmtree(staging_dir, ignore_errors=True)
    return sorted(output_dir.glob("auto_*.png")), None


def _build_crops_pdf(crop_paths: list[Path], output_path: Path) -> tuple[Path | None, str | None]:
    """把切片图按顺序合并成一个 PDF：每张切片占一页，页面尺寸 = 图片像素尺寸。

    output_path 已存在则直接返回（缓存），避免每次点下载都重新打包。
    """
    if not crop_paths:
        return None, "没有可合并的切片。"
    if output_path.exists():
        return output_path, None
    if not PYMUPDF_AVAILABLE:
        return None, "缺少 PyMuPDF，无法生成合并 PDF。"

    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None  # 允许打开接近 100M 像素的大图（doc15 整页那张）

    staging = output_path.with_suffix(".pdf.tmp")
    try:
        out_doc = fitz.open()
        try:
            for crop_path in crop_paths:
                with Image.open(crop_path) as im:
                    w, h = im.size
                # 页面尺寸用图片原始像素（1pt = 1px 即可，矢量图查看器无 DPI 概念）
                page = out_doc.new_page(width=w, height=h)
                page.insert_image(page.rect, filename=str(crop_path))
            out_doc.save(str(staging), garbage=4, deflate=True, deflate_images=True)
        finally:
            out_doc.close()
        staging.rename(output_path)
    except Exception as e:
        staging.unlink(missing_ok=True)
        return None, f"合并 PDF 失败：{e}"
    return output_path, None


def _get_dwg_documents(project: Project) -> list[Document]:
    """获取项目中已上传的 DWG 文件."""
    with session() as s:
        docs = (
            s.query(Document)
            .filter(
                Document.project_id == project.id,
                Document.category == "03_drawings",
            )
            .order_by(Document.uploaded_at.desc())
            .all()
        )
        return [d for d in docs if (d.filename or "").lower().endswith(".dwg")]


def _render_panzoom_iframe(image_path: Path, key: str, height: int = 600) -> None:
    """把图片以 base64 内联到 HTML，用 panzoom 嵌入成可平移缩放查看器。

    - 鼠标滚轮 = 缩放（以鼠标位置为中心）
    - 拖拽 = 平移
    - 右下角浮窗 = 缩放/重置/全屏按钮
    - 缩放范围 0.1× ~ 20×
    """
    img_b64 = base64.b64encode(image_path.read_bytes()).decode()
    html = f"""<!doctype html>
<html><head><meta charset="utf-8">
<style>
  html, body {{ margin: 0; padding: 0; height: 100%; background: #2b2b2b; }}
  #pz-{key} {{
    width: 100%; height: {height}px; background: #f5f5f5;
    overflow: hidden; position: relative; cursor: grab;
    border: 1px solid #ccc; border-radius: 4px;
    user-select: none; -webkit-user-select: none;
  }}
  #pz-{key}:active {{ cursor: grabbing; }}
  #pz-{key} img {{
    display: block; transform-origin: 0 0; will-change: transform;
    max-width: none; max-height: none; image-rendering: -webkit-optimize-contrast;
  }}
  .pz-ctrl {{
    position: absolute; bottom: 12px; right: 12px;
    display: flex; flex-direction: column; gap: 4px; z-index: 10;
  }}
  .pz-ctrl button {{
    width: 36px; height: 36px; border: none; border-radius: 4px;
    background: rgba(255,255,255,0.92); cursor: pointer; font-size: 18px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.3); line-height: 1;
  }}
  .pz-ctrl button:hover {{ background: #fff; }}
  .pz-info {{
    position: absolute; top: 8px; left: 8px;
    background: rgba(0,0,0,0.55); color: #fff;
    padding: 4px 10px; border-radius: 4px; font: 12px/1.4 monospace;
    pointer-events: none; z-index: 10;
  }}
</style>
</head>
<body>
<div id="pz-{key}">
  <img id="img-{key}" src="data:image/png;base64,{img_b64}">
  <div class="pz-info" id="info-{key}">缩放 100%</div>
  <div class="pz-ctrl">
    <button onclick="zoomIn()" title="放大">+</button>
    <button onclick="zoomOut()" title="缩小">−</button>
    <button onclick="reset()" title="重置">⤾</button>
  </div>
</div>
<script src="https://cdn.jsdelivr.net/npm/panzoom@9.4.0/dist/panzoom.min.js"></script>
<script>
  const wrap = document.getElementById('pz-{key}');
  const img  = document.getElementById('img-{key}');
  const info = document.getElementById('info-{key}');
  // 等图片加载完再算初始 fit
  img.onload = function() {{
    const sx = wrap.clientWidth  / img.naturalWidth;
    const sy = wrap.clientHeight / img.naturalHeight;
    const s0 = Math.min(sx, sy) * 0.95;
    const pz = panzoom(wrap, {{
      minScale: 0.05, maxScale: 20, bounds: false, smoothScroll: false,
      zoomDoubleClickSpeed: 1, beforeWheel: function(e){{ e.preventDefault(); }},
    }}, {{ contain: false }});
    // panzoom 是绑在 wrap 上的；但内部 transform 会被加到第一个子元素。
    // 我们把 transform 改挂到 img 自身。
    pz.dispose();
    const pz2 = panzoom(img, {{
      minScale: 0.05, maxScale: 20, bounds: false, smoothScroll: false,
      zoomDoubleClickSpeed: 1,
    }});
    pz2.zoomAbs(0, 0, s0);
    pz2.moveTo((wrap.clientWidth  - img.naturalWidth  * s0) / 2,
              (wrap.clientHeight - img.naturalHeight * s0) / 2);
    function upd(){{ info.textContent = '缩放 ' + Math.round(pz2.getScale() * 100) + '%'; }}
    img.addEventListener('panzoomchange', upd);
    window.zoomIn  = () => pz2.zoomTo(0, 0, pz2.getScale() * 1.25);
    window.zoomOut = () => pz2.zoomTo(0, 0, pz2.getScale() / 1.25);
    window.reset   = () => {{
      pz2.zoomAbs(0, 0, s0);
      pz2.moveTo((wrap.clientWidth  - img.naturalWidth  * s0) / 2,
                (wrap.clientHeight - img.naturalHeight * s0) / 2);
    }};
    upd();
    // 阻止 wrap 上的滚轮冒泡到外层 streamlit 容器
    wrap.addEventListener('wheel', e => e.stopPropagation(), {{ passive: true }});
  }};
  if (img.complete) img.onload();
</script>
</body></html>"""
    st.components.v1.html(html, height=height + 20, scrolling=False)


def _render_pdf_preview(project_id: int, doc: Document):
    """展示 DXF 缓存状态、转换进度、PDF 预览与下载。"""
    dwg_path = _doc_file_path(project_id, doc)
    if not dwg_path.exists():
        st.error(f"DWG 文件不存在：{dwg_path}")
        return

    # 原始 DWG 始终可下载
    col_dl1, col_dl2 = st.columns(2)
    with col_dl1:
        st.download_button(
            label="⬇️ 下载原始 DWG",
            data=dwg_path.read_bytes(),
            file_name=doc.filename,
            mime="application/acad",
            key=f"download_dwg_{doc.id}",
            use_container_width=True,
        )

    # macOS 上显示 DXF 缓存状态；其他平台自动转换无需缓存
    if _is_macos():
        cache_dir = _doc_dxf_cache_dir(doc)
        cached_dxf = _find_cached_dxf(doc)
        if cached_dxf:
            mtime_str = format_beijing(
                datetime.fromtimestamp(cached_dxf.stat().st_mtime),
                "%Y-%m-%d %H:%M", fallback="?",
            )
            st.success(
                f"✅ 已检测到缓存 DXF：`{cached_dxf.name}` "
                f"（{cached_dxf.stat().st_size / 1024:.1f} KB，{mtime_str}）"
            )
        else:
            st.info(
                f"⏳ 暂未检测到缓存 DXF。\n\n"
                f"请把 ODA File Converter 转出的 DXF 放到缓存目录：\n\n"
                f"`{cache_dir}/`\n\n"
                f"缓存目录已自动创建。把同名 .dxf 文件丢进去后**刷新本页**即可生成 PDF。"
            )
            return

    with st.spinner("正在生成 PDF..."):
        pdf_path, err = _convert_dwg_to_pdf(dwg_path, project_id, doc)

    if err or pdf_path is None:
        st.error(err or "转换失败。")
        return

    with col_dl2:
        st.download_button(
            label="⬇️ 下载转换后 PDF",
            data=pdf_path.read_bytes(),
            file_name=f"{Path(doc.filename).stem}.pdf",
            mime="application/pdf",
            key=f"download_pdf_{doc.id}",
            use_container_width=True,
        )

    if st.button("🔄 重新生成 PDF（清缓存重转，下游预览一起重算）",
                 key=f"regen_pdf_{doc.id}"):
        out_dir = _dwg2pdf_dir(project_id, doc.id)
        cached_pdf = out_dir / f"{dwg_path.stem}.pdf"
        if cached_pdf.exists():
            cached_pdf.unlink()
        for sub in ("pages", "previews"):
            d = out_dir / sub
            if d.exists():
                shutil.rmtree(d)
        st.rerun()

    with st.spinner("正在生成可平移缩放的高清预览..."):
        previews, prev_err = _generate_panzoom_previews(pdf_path, project_id, doc)

    if prev_err:
        st.warning(prev_err)
        return

    st.divider()
    st.subheader("🔍 全图预览（可平移缩放）")
    st.caption("鼠标滚轮缩放、拖拽平移；右下角按钮可重置/放大/缩小。基于 5000px 长边渲染，矢量精度任意缩放不模糊")

    if len(previews) == 1:
        page_number = 1
    else:
        page_number = st.selectbox(
            f"共 {len(previews)} 页，选择页码",
            range(1, len(previews) + 1),
            key=f"dwg2pdf_preview_page_{doc.id}",
        )
    preview_path = previews[page_number - 1]
    _render_panzoom_iframe(preview_path, key=f"pz_{doc.id}_{page_number}")



def _render_platform_help():
    """根据当前平台显示转换流程说明。"""
    if _is_macos():
        return [
            "**当前平台：macOS**（ODA File Converter 是 GUI 应用，无法 CLI 自动化）",
            "",
            "每个新 DWG 走一次手动流程（DXF 缓存长期复用）：",
            "",
            "1. 打开 ODA File Converter（已装在 `/Applications/ODAFileConverter.app`）",
            "2. **输入目录**：选择上传的 DWG 文件所在目录（项目 `documents/03_drawings/`）",
            "3. **输出目录**：选择下方“DXF 缓存目录”路径",
            "4. **输出版本**：`ACAD2018`，**输出文件类型**：`DXF`",
            "5. 勾选 **Recurse**、**Audit**，点 **Convert**",
            "6. 把生成的 `.dxf` 文件**复制一份**放到下方“DXF 缓存目录”对应子目录里",
            "7. 刷新本页，PDF 预览就出来了",
            "",
            "**DXF 缓存目录（统一放这里）**：",
        ]
    lines = [f"**当前平台：{platform.system()}**", ""]

    if _jenkins_available():
        lines += [
            "✅ Jenkins dwg2pdf job 已启用（质量接近 CAD 原生效果）",
            "",
        ]

    oda_cli = _find_oda_cli()
    if oda_cli:
        lines += [
            f"✅ 已找到 ODA File Converter CLI：`{oda_cli}`",
            "",
        ]

    if _jenkins_available() or oda_cli:
        lines += [
            "上传 DWG 后会自动（按优先级）：",
        ]
        steps = []
        if _jenkins_available():
            steps.append("1. Jenkins dwg2pdf job：GUI 自动化转换 → 打包 zip 回传（质量最好）")
        if oda_cli:
            steps.append(f"{len(steps)+1}. ODA CLI + ezdxf：DWG → DXF → 多页 PDF（本地兜底）")
        steps.append(f"{len(steps)+1}. PyMuPDF 拆页：PDF → PNG 预览")
        lines += steps
        lines += [
            "",
            "**完全自动，无需手动操作。**",
        ]
    else:
        lines += [
            "未找到 ODA File Converter CLI，也未配置 Jenkins，转换可能失败。",
            "",
            "启用方式（任选一种）：",
            "- 配置 `JENKINS_URL / JENKINS_USER / JENKINS_TOKEN / JENKINS_JOB_URL` 环境变量走 Jenkins job",
            "- 下载 ODA File Converter（Linux/Windows 版）放到 `/usr/bin/ODAFileConverter`",
            "- 或设置环境变量 `ODA_CLI_BIN` 指向它",
            "",
            "然后重启应用即可全自动转换。",
        ]

    return lines


def view_dwg2pdf(project: Project | None):
    """DWG 转 PDF 主页面：上传 DWG → 自动/手动转 PDF → 页面预览，原始 DWG 仅供下载."""
    if not project:
        st.warning("请先选择一个项目")
        return

    st.title("📐 DWG 转 PDF")
    st.caption(
        f"项目: {project.name} · 上传 DWG → 转换 PDF → 页面预览；原始 DWG 仅供下载。"
    )

    with st.expander("ℹ️ 转换流程说明", expanded=False):
        st.markdown("\n".join(_render_platform_help()))
        if _is_macos():
            st.code(str(DXF_CACHE_ROOT.resolve()), language="text")
            st.caption(
                f"每个 DWG 对应一个子目录：`{DXF_CACHE_ROOT.name}/doc_<id>_<dwg名>/<dwg名>.dxf`"
            )

    docs = _get_dwg_documents(project)

    source_mode = st.radio(
        "DWG 来源",
        ["上传新的 DWG 文件", "选择已上传 DWG"],
        index=1 if docs else 0,
        horizontal=True,
        key="dwg2pdf_source_mode",
    )

    selected_doc = None

    if source_mode == "选择已上传 DWG":
        if not docs:
            st.info("暂无已上传的 DWG 文件，请切换到「上传新的 DWG 文件」。")
            return
        options = [f"{d.filename}" for d in docs]
        idx = st.selectbox(
            "选择 DWG 文件",
            range(len(options)),
            format_func=lambda i: options[i],
            key="dwg2pdf_doc_selector",
        )
        selected_doc = docs[idx]
    else:
        uploaded_file = st.file_uploader(
            "上传 DWG 文件",
            type=["dwg"],
            accept_multiple_files=False,
            key="dwg2pdf_upload_file",
            help="上传后保存到项目图纸分类。",
        )
        if st.button("上传", type="primary", disabled=uploaded_file is None):
            with st.spinner("正在保存 DWG 文件..."):
                doc_id = _save_uploaded_dwg(project.id, uploaded_file)
            st.session_state["dwg2pdf_selected_doc_id"] = doc_id
            st.success("DWG 已上传。")
            st.rerun()

        selected_doc_id = st.session_state.get("dwg2pdf_selected_doc_id")
        if selected_doc_id:
            docs = _get_dwg_documents(project)
            selected_doc = next((d for d in docs if d.id == selected_doc_id), None)

        if selected_doc is None:
            st.info("请先上传一个 DWG 文件。")
            return

    st.divider()
    st.subheader("📄 文件信息")
    upload_time = format_beijing(selected_doc.uploaded_at, "%Y-%m-%d %H:%M:%S", fallback="未知")
    st.markdown(f"**文件名:** {selected_doc.filename}")
    st.markdown(f"**上传时间:** {upload_time}")
    st.markdown(f"**文件大小:** {selected_doc.size_bytes / 1024:.1f} KB")

    st.divider()
    _render_pdf_preview(project.id, selected_doc)
