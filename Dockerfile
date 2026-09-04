# 基于 Python 3.11（固定 bookworm，保证 apt 包名稳定）
FROM docker.m.daocloud.io/library/python:3.11-slim-bookworm

# 国内构建加速（海外构建时可用 --build-arg 关闭/覆盖）
ARG USE_CN_APT_MIRROR=true
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

# 让后续所有 pip 命令默认走该源（torch wheel 用 curl 单独下载）
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=5

RUN if [ "$USE_CN_APT_MIRROR" = "true" ]; then \
        sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' \
            /etc/apt/sources.list.d/debian.sources; \
    fi

# 安装系统依赖
#   poppler-utils / tesseract  —— OCR 与 pdf2image
#   libgl1 / libglib2.0-0      —— opencv-python 运行时
#   fonts-noto-cjk             —— 中文渲染（tesseract、matplotlib、LibreOffice 转 PDF）
#   libreoffice-draw           —— DXF → PDF（soffice --convert-to pdf，无头模式）
#   xvfb + libxcb*/libxkbcommon  —— ODA File Converter CLI 运行依赖（Qt xcb 平台插件）
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    tesseract-ocr \
    tesseract-ocr-chi-sim \
    tesseract-ocr-chi-tra \
    libgl1 \
    libglib2.0-0 \
    fonts-noto-cjk \
    libreoffice-draw \
    xvfb \
    xauth \
    libxkbcommon0 \
    libxkbcommon-x11-0 \
    libxcb-icccm4 \
    libxcb-image0 \
    libxcb-keysyms1 \
    libxcb-randr0 \
    libxcb-render-util0 \
    libxcb-shape0 \
    libxcb-xinerama0 \
    libxcb-xkb1 \
    libxcb-util1 \
    libdbus-1-3 \
    libfontconfig1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ---- ODA File Converter CLI（DWG → DXF 自动转换）----
# 官方只提供 rpm，Debian 系用 rpm2cpio 解包手动布置（构建完即清理，不进最终镜像体积）。
# 注意：ODA CLI 是 Qt 应用且只带 xcb 平台插件，无头环境直接跑会报
# "no Qt platform plugin could be initialized"，必须套 xvfb-run 虚拟显示。
COPY ODAFileConverter_QT6_lnxX64_8.3dll_27.1.rpm /tmp/oda.rpm
RUN set -e; \
    apt-get update \
    && apt-get install -y --no-install-recommends rpm2cpio cpio \
    && mkdir -p /tmp/oda && cd /tmp/oda \
    && rpm2cpio /tmp/oda.rpm | cpio -idm --quiet \
    && cp -r usr/local/bin/ODAFileConverter_* /usr/local/bin/ \
    && ODA_DIR=$(ls -d /usr/local/bin/ODAFileConverter_* | head -1) \
    && printf '#!/bin/sh\nexec xvfb-run -a %s/ODAFileConverter "$@"\n' "$ODA_DIR" \
        > /usr/local/bin/ODAFileConverter \
    && chmod +x /usr/local/bin/ODAFileConverter \
    && apt-get purge -y rpm2cpio cpio \
    && rm -rf /tmp/oda /tmp/oda.rpm /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app

# 复制依赖文件（单一来源：requirements.txt）
COPY requirements.txt ./

# 先装 CPU 版 torch/torchvision（避免 PyPI 默认 CUDA wheel 连带 2GB+ nvidia 依赖）。
# 注意：官方 index 里的下载链接指向 download-r2.pytorch.org（R2 CDN），国内常被限速到
# 几 kB/s 导致 pip 超时；同样的文件在 download.pytorch.org 主站上可直接下载且支持断点
# 续传，因此改用 curl（慢速熔断 + 断点续传重试）拉 wheel 后本地安装。
ARG TORCH_VER=2.13.0
ARG TORCHVISION_VER=0.28.0
ARG PY_TAG=cp311
RUN set -e; \
    # URL 里版本号含 %2B（即 + 的转义），但本地 wheel 文件名必须还原为 +，\
    # 否则 pip 会报 "not a valid wheel filename" \
    U_T="torch-${TORCH_VER}%2Bcpu-${PY_TAG}-${PY_TAG}-manylinux_2_28_x86_64.whl"; \
    U_V="torchvision-${TORCHVISION_VER}%2Bcpu-${PY_TAG}-${PY_TAG}-manylinux_2_28_x86_64.whl"; \
    T="torch-${TORCH_VER}+cpu-${PY_TAG}-${PY_TAG}-manylinux_2_28_x86_64.whl"; \
    V="torchvision-${TORCHVISION_VER}+cpu-${PY_TAG}-${PY_TAG}-manylinux_2_28_x86_64.whl"; \
    for pair in "$U_T $T" "$U_V $V"; do \
        set -- $pair; u="$1"; f="$2"; \
        ok=""; \
        for i in 1 2 3 4 5; do \
            # 低于 10KB/s 持续 30s 视为被限速，断开重试（-C - 断点续传）
            if curl -fL -C - --connect-timeout 15 \
                    --speed-limit 10240 --speed-time 30 \
                    -o "/tmp/$f" "https://download.pytorch.org/whl/cpu/$u"; then \
                ok=1; break; \
            fi; \
            sleep 5; \
        done; \
        [ -n "$ok" ] || { echo "下载失败: $u"; exit 1; }; \
    done; \
    # numpy 先限定 <2（requirements.txt 的约束），避免 torchvision 拉进 numpy 2.x
    pip install --no-cache-dir "numpy>=1.26,<2.0" "/tmp/$T" "/tmp/$V"; \
    rm -f "/tmp/$T" "/tmp/$V"

# 再装其余 Python 依赖（torch 已满足，不会重复拉取）
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码（.dockerignore 已排除 .git/.venv/data/outputs 等）
COPY . .

# 创建数据目录（docker-compose 挂载 ./data ./outputs 时以宿主机为准）
RUN mkdir -p data projects outputs

ENV PYTHONUNBUFFERED=1 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
# ODA File Converter CLI 已内置于 /usr/local/bin/ODAFileConverter（自动套 xvfb-run），
# 应用按 PATH 查找即可命中；如挂载其它版本可用 ODA_CLI_BIN 覆盖
# ENV ODA_CLI_BIN=/opt/ODAFileConverter/ODAFileConverter

# 暴露 Streamlit 端口
EXPOSE 8501

# 健康检查
HEALTHCHECK CMD curl --fail http://localhost:8501/_stcore/health || exit 1

# 启动命令
CMD ["streamlit", "run", "app/streamlit_app.py", "--server.port=8501", "--server.address=0.0.0.0"]
