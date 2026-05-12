# 基于 Python 3.11
FROM python:3.11-slim

# 安装系统依赖（OCR 所需）
RUN apt-get update && apt-get install -y \
    poppler-utils \
    tesseract-ocr \
    tesseract-ocr-chi-sim \
    tesseract-ocr-chi-tra \
    libgl1-mesa-glx \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app

# 复制依赖文件
COPY pyproject.toml ./

# 安装 Python 依赖
RUN pip install --no-cache-dir \
    streamlit \
    sqlalchemy \
    numpy \
    openai \
    transformers \
    torch \
    pypdf \
    python-docx \
    python-pptx \
    openpyxl \
    pandas \
    bcrypt \
    opencv-python \
    plotly \
    pdf2image \
    pytesseract \
    pillow

# 复制应用代码
COPY . .

# 创建数据目录
RUN mkdir -p data projects outputs

# 暴露 Streamlit 端口
EXPOSE 8501

# 健康检查
HEALTHCHECK CMD curl --fail http://localhost:8501/_stcore/health || exit 1

# 启动命令
CMD ["streamlit", "run", "app/streamlit_app.py", "--server.port=8501", "--server.address=0.0.0.0"]
