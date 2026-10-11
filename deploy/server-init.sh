#!/usr/bin/env bash
# ============================================================
# 服务器一次性初始化（**在服务器上**执行，脚本会被传到 /opt/stock）
#
# 做四件事：swap / 目录权限 / 时区 / 磁盘确认
# ============================================================
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
PROJECT_DIR="$(pwd)"
SWAP_SIZE="${SWAP_SIZE:-4G}"

echo "▸ 1/4 配置 swap（${SWAP_SIZE}）"
# ★ 为什么要：模拟盘冷启动会走**全量重算**，实测峰值 3383MB；4GB 物理内存
#   在那一刻会挤爆 → OOM killer 直接杀进程，净值停在半路。swap 是唯一兜底。
if swapon --show | grep -q "/swapfile"; then
  echo "  已启用，跳过"
else
  sudo fallocate -l "$SWAP_SIZE" /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile > /dev/null
  sudo swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
  echo "  ✓ 已启用"
fi
free -h | sed -n '1,3p'
echo

echo "▸ 2/4 目录与权限"
mkdir -p data/meta data/paper data/factor backend/logs
# 容器以 root 跑，写入挂载卷没问题；这里确保目录存在即可
echo "  ✓ $PROJECT_DIR/data 就绪"
echo

echo "▸ 3/4 时区（调度 cron 全按北京时间）"
sudo timedatectl set-timezone Asia/Shanghai
timedatectl | sed -n '1,4p'
echo

echo "▸ 4/4 磁盘与内核参数"
df -h "$PROJECT_DIR" | sed -n '1,2p'
# 大量小文件读写（parquet）时，默认的 inode 缓存偏小
sudo sysctl -w vm.swappiness=10 > /dev/null && echo "  ✓ vm.swappiness=10（少用 swap，多用物理内存）"
echo

echo "✓ 初始化完成"
echo
echo "启动："
echo "  docker compose -f deploy/docker-compose.yml up -d --build"
echo "看日志："
echo "  docker compose -f deploy/docker-compose.yml logs -f backend"
echo "前端访问："
echo "  http://\$(curl -s ifconfig.me)/  （记得在阿里云安全组放行 80 端口）"