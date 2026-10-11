#!/usr/bin/env bash
# ============================================================
# 生成 Basic Auth 密码文件（**在本地**跑，生成 deploy/.htpasswd）
#
# 用法：bash deploy/gen-auth.sh
# 会交互式询问用户名与密码（密码输入不回显），生成后随 upload-to-server.sh
# 一起传到服务器。改密码 = 重跑本脚本 + 重传 + 重启前端容器。
#
# ⚠️ 已知取舍：Basic Auth 走 HTTP 明文（Base64，非加密）。防的是"知道 IP
#   的陌生人随便看"，防不了中间人截获。要彻底解决需要域名 + HTTPS，后续可加。
# ============================================================
set -euo pipefail

OUT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.htpasswd"

if ! command -v openssl > /dev/null; then
  echo "错误：找不到 openssl（Git Bash 自带，正常不会缺）"
  exit 1
fi

read -r -p "访问用户名（默认 admin）: " USER_NAME
USER_NAME="${USER_NAME:-admin}"

while true; do
  read -r -s -p "访问密码（输入不回显，至少 8 位）: " PASS1
  echo
  read -r -s -p "再输一遍确认: " PASS2
  echo
  if [ "${#PASS1}" -lt 8 ]; then
    echo "✗ 密码太短（<8 位），重来"
    continue
  fi
  if [ "$PASS1" != "$PASS2" ]; then
    echo "✗ 两次不一致，重来"
    continue
  fi
  break
done

HASH=$(openssl passwd -apr1 "$PASS1")
printf '%s:%s\n' "$USER_NAME" "$HASH" > "$OUT"
# ★ 644 而不是 600：nginx **worker 进程**（alpine 镜像里是 nginx 用户，非 root）
#   按需读取该文件，600（root-only）会让所有请求 500 "Internal Server Error"。
#   文件内容只是 apr1 哈希（不可逆，需暴力破解），服务器上 644 可接受；
#   本地这份已进 .gitignore，不会外传。
chmod 644 "$OUT"
echo "✓ 已生成 $OUT（用户：$USER_NAME）"
echo "  该文件已加入 .gitignore，不会进 git；upload-to-server.sh 会自动带上服务器。"