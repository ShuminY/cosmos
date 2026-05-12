# Cosmos 部署指南

## 方式一：Docker 部署（推荐）

### 1. 服务器准备

确保服务器已安装：
- Docker
- Docker Compose

```bash
# 检查 Docker
docker --version
docker-compose --version
```

### 2. 部署步骤

```bash
# 克隆代码
git clone <your-repo-url>
cd cosmos

# 构建并启动
docker-compose up -d --build

# 查看日志
docker-compose logs -f

# 停止
docker-compose down
```

### 3. 访问应用

- 本地：http://localhost:8501
- 服务器：http://<服务器IP>:8501

---

## 方式二：裸机部署

### 1. 环境要求

- Python 3.11
- poppler-utils（PDF 处理）
- tesseract-ocr（OCR）

### 2. Ubuntu/Debian 安装

```bash
# 系统依赖
sudo apt-get update
sudo apt-get install -y \
    python3.11 python3.11-venv \
    poppler-utils \
    tesseract-ocr \
    tesseract-ocr-chi-sim \
    libgl1-mesa-glx

# 克隆代码
git clone <your-repo-url>
cd cosmos

# 创建虚拟环境
python3.11 -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 启动
streamlit run app/streamlit_app.py --server.port 8501 --server.address 0.0.0.0
```

### 3. 使用 systemd 守护进程

创建服务文件 `/etc/systemd/system/cosmos.service`：

```ini
[Unit]
Description=Cosmos Construction Progress POC
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/cosmos
Environment=PATH=/home/ubuntu/cosmos/.venv/bin
ExecStart=/home/ubuntu/cosmos/.venv/bin/streamlit run app/streamlit_app.py --server.port 8501 --server.address 0.0.0.0
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

启用并启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable cosmos
sudo systemctl start cosmos
sudo systemctl status cosmos
```

---

## 方式三：云服务部署

### 阿里云 ECS

1. 创建 ECS 实例（建议 2C4G 以上）
2. 开放安全组端口 8501
3. 按上述 Docker 或裸机方式部署

### 使用 Nginx 反向代理

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://localhost:8501;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

---

## 数据备份

重要数据目录：
- `data/` - SQLite 数据库和上传的文件
- `outputs/` - 处理结果

备份命令：

```bash
# 备份
tar -czf backup-$(date +%Y%m%d).tar.gz data/ outputs/

# 恢复
tar -xzf backup-20260101.tar.gz
```

---

## 常见问题

### 1. OCR 中文识别失败

确保安装了中文语言包：

```bash
# Docker 中已包含
# 裸机部署时：
sudo apt-get install tesseract-ocr-chi-sim tesseract-ocr-chi-tra
```

### 2. 内存不足

减少 embedding 维度或限制同时处理的文档数。

### 3. 端口被占用

```bash
# 查看端口占用
sudo lsof -i :8501

# 更换端口启动
streamlit run app/streamlit_app.py --server.port 8502
```
