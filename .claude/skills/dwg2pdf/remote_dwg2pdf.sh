#!/bin/bash
# remote_dwg2pdf.sh — 服务器 dwg 转pdf 一条龙：下载 → 本机转换 → zip 传回
# 用法: remote_dwg2pdf.sh <服务器上的dwg完整路径> [更多路径...]
# 例:   remote_dwg2pdf.sh /data/upload/01-封面.dwg
# 依赖: 本机 GUI 会话 + 辅助访问/屏幕录制权限 + dwg2pdf.sh；传回用 scp（密钥认证）
set -euo pipefail

# ==================== 服务器信息（自己填写） ====================
SERVER_HOST="test.aleph-lop.com"          # 例 192.168.1.100 或 files.example.com
SERVER_PORT=22
SERVER_USER="lop"          # 例 root / deploy

# 密钥认证：填私钥文件路径（留空则用默认 ~/.ssh/id_* 或 ssh-agent）
SERVER_KEY="$HOME/.ssh/lop.key"

REMOTE_OUT_DIR=""       # 传回目标目录；留空 = 传回 dwg 所在的远端目录
# ===============================================================

# 本机转换脚本（独立安装后改成 ~/bin/dwg2pdf）
DWG2PDF="${DWG2PDF:-/Users/wangningkang/IdeaProjects/cosmos/.claude/skills/dwg2pdf/dwg2pdf.sh}"

[ -n "$SERVER_HOST" ] && [ -n "$SERVER_USER" ] || { echo "错误: 请先在脚本头部填写 SERVER_HOST / SERVER_USER"; exit 1; }
[ -x "$DWG2PDF" ] || [ -f "$DWG2PDF" ] || { echo "错误: 找不到转换脚本 $DWG2PDF"; exit 1; }
if [ -n "$SERVER_KEY" ] && [ ! -f "$SERVER_KEY" ]; then
  echo "错误: 找不到密钥文件 $SERVER_KEY"; exit 1
fi

SSH_OPTS=(-P "$SERVER_PORT" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)
[ -n "$SERVER_KEY" ] && SSH_OPTS+=(-i "$SERVER_KEY")

# 统一的 scp 封装（密钥认证）
scp_put() { # $1=本地路径 $2=远端规格
  scp "${SSH_OPTS[@]}" "$1" "$2"
}
scp_get() { # $1=远端规格 $2=本地路径
  scp "${SSH_OPTS[@]}" "$1" "$2"
}

# ---------- 参数检查 ----------
[ $# -ge 1 ] || { echo "用法: remote_dwg2pdf.sh <服务器dwg完整路径> [更多路径...]"; exit 1; }

OK=0; FAIL=0

for REMOTE_FILE in "$@"; do
  REMOTE_DIR="${REMOTE_FILE%/*}"; REMOTE_NAME="${REMOTE_FILE##*/}"
  BASE="${REMOTE_NAME%.*}"
  [ -n "$REMOTE_OUT_DIR" ] && OUT_DIR="$REMOTE_OUT_DIR" || OUT_DIR="$REMOTE_DIR"

  echo "==== [$BASE] 下载 $REMOTE_FILE ===="
  WORK=$(mktemp -d /tmp/remote_dwg2pdf.XXXXXX)
  # 新版 OpenSSH scp 默认走 SFTP 协议，远端路径直接传即可（无 shell 展开，不要手动加引号）
  if ! scp_get "$SERVER_USER@$SERVER_HOST:$REMOTE_FILE" "$WORK/$REMOTE_NAME"; then
    echo "✖ 下载失败"; rm -rf "$WORK"; FAIL=$((FAIL+1)); continue
  fi

  echo "==== [$BASE] 本机转换（GUI 自动化，期间别动鼠标键盘）===="
  if ! bash "$DWG2PDF" "$WORK/$REMOTE_NAME"; then
    echo "✖ 转换失败"; rm -rf "$WORK"; FAIL=$((FAIL+1)); continue
  fi

  # 归档产物：<base>.zip；兜底：若没 zip 但有散落 pdf，全部传回
  UPLOADS=()
  [ -e "$WORK/$BASE.zip" ] && UPLOADS+=("$WORK/$BASE.zip")
  if [ ${#UPLOADS[@]} -eq 0 ]; then
    while IFS= read -r f; do UPLOADS+=("$f"); done < <(find "$WORK" -name '*.pdf' -maxdepth 1)
  fi
  if [ ${#UPLOADS[@]} -eq 0 ]; then
    echo "⚠ 无任何 PDF 产出（可能所有布局都无图框），不传回"; rm -rf "$WORK"; FAIL=$((FAIL+1)); continue
  fi

  echo "==== [$BASE] 传回 $OUT_DIR/ ===="
  SENT=0
  for f in "${UPLOADS[@]}"; do
    if scp_put "$f" "$SERVER_USER@$SERVER_HOST:$OUT_DIR/"; then
      echo "   ✔ $(basename "$f")"
      SENT=$((SENT+1))
    else
      echo "   ✖ 上传失败: $(basename "$f")"
    fi
  done
  rm -rf "$WORK"
  if [ "$SENT" -gt 0 ]; then OK=$((OK+1)); else FAIL=$((FAIL+1)); fi
done

echo "==== 完成: 成功 $OK 个, 失败 $FAIL 个 ===="
