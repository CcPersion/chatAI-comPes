#!/bin/bash
# ============================================================
# AI Voice Companion — 一键启动所有服务
# 在 WSL2 中运行: bash /mnt/d/codexWorkSpec/chatAI-comPes/scripts/start-all.sh
# ============================================================

set -e

echo "=== 停止旧服务 ==="
fuser -k 7860/tcp 2>/dev/null || true
fuser -k 8010/tcp 2>/dev/null || true
fuser -k 8011/tcp 2>/dev/null || true
sleep 2

echo "=== 启动 LiveTalking (8010) ==="
cd /root/setup/LiveTalking
nohup /root/setup/venv/bin/python app.py > /tmp/livetalking.log 2>&1 &
echo "  PID=$!"

echo "=== 启动 avatar-sync (8011) ==="
cd /root/setup
nohup node avatar-sync.js > /tmp/avatar-sync.log 2>&1 &
echo "  PID=$!"

echo "=== 启动 app.py (7860) ==="
cd /root/setup
nohup /root/setup/venv/bin/python app.py > /tmp/app.log 2>&1 &
echo "  PID=$!"

sleep 5

echo ""
echo "=== 服务状态 ==="
ss -tlnp | grep -E "7860|8010|8011" || echo "有服务未启动，请检查日志:"
echo "  LiveTalking:  tail /tmp/livetalking.log"
echo "  avatar-sync:  tail /tmp/avatar-sync.log"
echo "  app.py:       tail /tmp/app.log"
