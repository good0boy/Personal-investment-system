#!/usr/bin/env bash
# ============================================================
# 上传项目到服务器（在**本地** Git Bash 跑，不是在服务器上）
#
# 用法：
#     cd /f/股票投资服务
#     bash deploy/upload-to-server.sh root@8.130.104.166
#
# 传什么：
#   · 代码        ~15MB（排除 .venv/node_modules/.git/日志/前端产物）
#   · .env        密钥文件单独传（走 SSH，加密通道）
#   · data/       行情 + 基本面 + 因子 + 模拟盘产物（**必传**，否则服务器要花 15 小时自己补）
#   · 跳过        backtest/（服务器跑不了回测，白占 134MB）、frontend/dist/、*.log
#
# 断点续传：rsync 天然支持，中断后重跑同一条命令即可
# ============================================================
set -euo pipefail

SERVER="${1:?用法: bash deploy/upload-to-server.sh user@host}"
REMOTE_DIR="${REMOTE_DIR:-/opt/stock}"
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "本地目录 : $LOCAL_DIR"
echo "服务器   : $SERVER:$REMOTE_DIR/"
echo

# ---- 0. 前置检查：Basic Auth 密码文件必须先生成 ----
# （compose 把它挂载进 nginx；文件不存在时 Docker 会创建同名**目录**，
#   nginx 因读不到密码文件直接起不来。）
if [ ! -f "$LOCAL_DIR/deploy/.htpasswd" ]; then
  echo "✗ 缺少 deploy/.htpasswd —— 请先跑："
  echo "    bash deploy/gen-auth.sh"
  echo "  （设访问用户名密码，防止净值/持仓公开到公网）"
  exit 1
fi
echo "▸ Basic Auth 密码文件 ✓"

# ---- 0.5 环境探测：本地必须有 rsync ----
# Git for Windows（含 WorkBuddy 便携版）默认不带 rsync。安装方法见
# docs/部署指南.md §2.2.1（MSYS2 rsync 装到 ~/bin，无需管理员）。
if ! command -v rsync > /dev/null 2>&1; then
  echo "✗ 本地没有 rsync（Git Bash 默认不带）。两种解决："
  echo "  a) 按 docs/部署指南.md §2.2.1 安装到 ~/bin（推荐，支持断点续传）"
  echo "  b) 临时凑合：tar 管道直传（无断点续传，2.6GB 中断即重来）"
  exit 1
fi
echo "▸ rsync ✓ $(rsync --version | head -1)"

# ---- 1. 先在服务器上建好目录（避免 root 属主混乱）----
echo "▸ 初始化远端目录..."
ssh "$SERVER" "mkdir -p '$REMOTE_DIR/data' '$REMOTE_DIR/backend/logs' && echo '  ok'"
echo

# ---- 2. 同步代码与数据（排除不该进服务器的东西）----
# 说明（2026-10-11 整改）：
#   · 排除 .idea/.workbuddy：本地 IDE/工作助手配置，服务器用不上；
#   · data/backtest + data/sweep **要同步**：前端「策略回测/参数寻优」页是纯读
#     接口，服务器没这两份产物页面就是空白（134M + 841K，零头而已）；
#   · .env **必须同步**：compose 的 env_file 从服务器项目根读它
#     （FUYAO_API_KEY / 企微 webhook 都在里面）。同步完自动收紧为 600。
echo "▸ 同步项目（约 2.7GB，视带宽 1~3 小时；可随时 Ctrl-C，重跑即续传）..."
rsync -avz \
  --delete \
  --partial \
  --human-readable \
  --info=progress2 \
  --exclude='.git/' \
  --exclude='.venv/' \
  --exclude='node_modules/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='.pytest_cache/' \
  --exclude='.ruff_cache/' \
  --exclude='.idea/' \
  --exclude='.workbuddy/' \
  --exclude='backend/logs/' \
  --exclude='frontend/dist/' \
  --exclude='*.log' \
  "$LOCAL_DIR/" "$SERVER:$REMOTE_DIR/"
echo

# ---- 3. 密钥文件收紧权限（.env 里有 FUYAO_API_KEY / 企微凭据）----
echo "▸ 收紧服务器上 .env 权限（600）..."
ssh "$SERVER" "chmod 600 '$REMOTE_DIR/.env' && echo '  ok'"
echo

echo "✓ 同步完成"
echo
echo "下一步（在服务器上执行）："
echo "  ssh $SERVER"
echo "  bash $REMOTE_DIR/deploy/server-init.sh     # 配 swap + 目录 + 时区"
echo "  cd $REMOTE_DIR && docker compose -f deploy/docker-compose.yml up -d --build"