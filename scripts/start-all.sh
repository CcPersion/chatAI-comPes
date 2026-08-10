#!/bin/bash
# ============================================================
# AI Voice Companion — 一键启动所有服务
# 在 WSL2 中运行: bash /mnt/d/codexWorkSpec/chatAI-comPes/scripts/start-all.sh
# ============================================================

set -e

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MAIN_PYTHON="${MAIN_PYTHON:-$ROOT_DIR/venv/bin/python}"

if [[ ! -x "$MAIN_PYTHON" ]]; then
  echo "ERROR: main WSL Python environment is missing: $MAIN_PYTHON" >&2
  exit 2
fi

echo "=== 停止旧服务 ==="
fuser -k 7860/tcp 2>/dev/null || true
fuser -k 8010/tcp 2>/dev/null || true
fuser -k 8011/tcp 2>/dev/null || true
fuser -k 8020/tcp 2>/dev/null || true
sleep 2

echo "=== 启动 LiveTalking (8010) ==="
cd "$ROOT_DIR/LiveTalking"
nohup "$MAIN_PYTHON" app.py --transport webrtc --model wav2lip --avatar_id wav2lip256_avatar_256 --listenport 8010 > /tmp/livetalking.log 2>&1 &
echo "  PID=$!"

echo "=== 启动 avatar-sync (8011) ==="
cd "$ROOT_DIR"
nohup node avatar-sync.js > /tmp/avatar-sync.log 2>&1 &
echo "  PID=$!"

echo "=== 启动 VoxCPM Worker (8020) ==="
cd "$ROOT_DIR"
nohup bash "$ROOT_DIR/scripts/start-voxcpm-worker.sh" > /tmp/voxcpm-worker.log 2>&1 &
echo "  PID=$!"

echo "=== 启动 app.py (7860) ==="
cd "$ROOT_DIR"
nohup "$MAIN_PYTHON" "$ROOT_DIR/outputs/backend/app.py" > /tmp/app.log 2>&1 &
echo "  PID=$!"

sleep 5

echo ""
echo "=== 服务状态 ==="
ss -tlnp | grep -E "7860|8010|8011|8020" || echo "有服务未启动，请检查日志:"
echo "  LiveTalking:  tail /tmp/livetalking.log"
echo "  avatar-sync:  tail /tmp/avatar-sync.log"
echo "  VoxCPM Worker: tail /tmp/voxcpm-worker.log"
echo "  app.py:       tail /tmp/app.log"
