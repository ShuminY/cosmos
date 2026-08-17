"""DWG 转 PDF 页面 - 把上传的 DWG 转成 PDF，页面预览 PDF，原始 DWG 仅供下载。

平台自适应：
- Linux/Windows：自动调用 ODA File Converter CLI → ezdxf 渲染（一条龙全自动）
- macOS：ODA 是 GUI 应用无法 CLI，依赖用户在缓存目录中放入已转好的 DXF
"""
from __future__ import annotations
import os
import platform
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


def _dxf_to_pdf_via_ezdxf(dxf_path: Path, pdf_path: Path) -> tuple[bool, str]:
    """用 ezdxf + pymupdf 后端把 DXF 的所有 layout 渲染为多页 PDF（保留每张图纸的真实尺寸）。"""
    if not EZDXF_AVAILABLE:
        return False, "未安装 ezdxf，无法渲染 DXF。"

    try:
        doc = ezdxf.readfile(str(dxf_path))
    except Exception as e:
        return False, f"无法读取 DXF：{e}"

    layouts = [("Model", doc.modelspace())]
    for name in sorted(doc.layouts.names()):
        if name == "Model":
            continue
        layout = doc.layouts.get(name)
        if any(True for _ in layout):
            layouts.append((name, layout))

    if len(layouts) == 0:
        return False, "DXF 中没有可渲染的 layout"

    pdf_doc = fitz.open()
    rendered = 0
    for name, layout in layouts:
        try:
            ctx = RenderContext(doc)
            backend = PyMuPdfBackend()
            Frontend(ctx, backend).draw_layout(layout, finalize=True)
            pdf_bytes = backend.get_pdf_bytes()
            if not pdf_bytes:
                continue
            sub = fitz.open(stream=pdf_bytes, filetype="pdf")
            pdf_doc.insert_pdf(sub)
            sub.close()
            rendered += 1
        except Exception:
            continue

    if rendered == 0:
        return False, "所有 layout 渲染失败"

    pdf_doc.save(str(pdf_path))
    pdf_doc.close()
    return True, f"已渲染 {rendered} 个 layout"


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
        1. 自动调 ODA File Converter CLI：DWG → DXF（保留完整工程图）
        2. ezdxf 渲染 DXF 为多页 PDF（保留每张图纸真实尺寸）
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
    if not _is_macos() and oda_cli:
        # 1. DWG → DXF（用 ODA CLI）
        dxf_path = out_dir / f"{dwg_path.stem}.dxf"
        if not dxf_path.exists() or dxf_path.stat().st_size == 0:
            ok, err = _convert_dwg_to_dxf_via_oda_cli(dwg_path, dxf_path, oda_cli)
            if not ok:
                last_err = f"ODA CLI：{err}"

        if dxf_path.exists() and dxf_path.stat().st_size > 0:
            # 2. ezdxf 渲染（首选，保留真实尺寸）
            ok, msg = _dxf_to_pdf_via_ezdxf(dxf_path, pdf_path)
            if ok:
                return pdf_path, None
            last_err += f"；ezdxf：{msg}"
            # 3. 备份：LibreOffice 渲染
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
           "Linux/Windows 用户：请安装 ODA File Converter CLI 并设置环境变量 ODA_CLI_BIN，"
           "或安装 LibreOffice。")
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
            pix = page.get_pixmap(dpi=150, alpha=False)
            pix.save(str(output_dir / f"page_{page_index + 1:03d}.png"))
    except Exception as e:
        return [], f"PDF 拆页为 PNG 失败：{str(e)}"
    finally:
        pdf_doc.close()

    pages = sorted(output_dir.glob("page_*.png"))
    if not pages:
        return [], "PDF 拆页后未生成图片。"
    return pages, None


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

    with st.spinner("正在生成 PDF 逐页预览..."):
        pages, page_err = _convert_pdf_to_images(pdf_path, project_id, doc)

    if page_err:
        st.warning(page_err)
        return

    st.divider()
    st.subheader("📄 PDF 预览")

    if len(pages) == 1:
        st.image(str(pages[0]), use_container_width=True, caption="第 1 页")
        return

    page_number = st.selectbox(
        f"共 {len(pages)} 页，选择页码",
        range(1, len(pages) + 1),
        key=f"dwg2pdf_page_{doc.id}",
    )
    st.image(str(pages[page_number - 1]), use_container_width=True, caption=f"第 {page_number} 页 / 共 {len(pages)} 页")


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
    oda_cli = _find_oda_cli()
    if oda_cli:
        return [
            f"**当前平台：{platform.system()}**",
            "",
            f"✅ 已找到 ODA File Converter CLI：`{oda_cli}`",
            "",
            "上传 DWG 后会自动：",
            "1. ODA CLI：DWG → DXF（保留完整工程图）",
            "2. ezdxf 渲染：DXF → 多页 PDF（每张图纸真实尺寸）",
            "3. PyMuPDF 拆页：PDF → PNG 预览",
            "",
            "**完全自动，无需手动操作。**",
        ]
    return [
        f"**当前平台：{platform.system()}**",
        "",
        "未找到 ODA File Converter CLI，转换可能失败。",
        "",
        "安装方法：",
        "- 下载 ODA File Converter（Linux/Windows 版）放到 `/usr/bin/ODAFileConverter`",
        "- 或设置环境变量 `ODA_CLI_BIN` 指向它",
        "",
        "然后重启应用即可全自动转换。",
    ]


def view_dwg2pdf(project: Project | None):
    """DWG 转 PDF 主页面：上传 DWG → 自动/手动转 PDF → 页面预览，原始 DWG 仅供下载."""
    if not project:
        st.warning("请先选择一个项目")
        return

    st.title("📐 DWG 转 PDF")
    st.caption(
        f"项目: {project.name} · 上传 DWG → 转换 PDF → 页面预览；原始 DWG 仅供下载。"
    )

    with st.expander("ℹ️ 转换流程说明", expanded=True):
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
