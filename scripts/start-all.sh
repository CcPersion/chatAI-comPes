#!/bin/bash
# ============================================================
# AI Voice Companion — 一键启动所有服务
# 在 WSL2 中运行: bash /mnt/d/codexWorkSpec/chatAI-comPes/scripts/start-all.sh
# ============================================================

set -e

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MAIN_PYTHON="${MAIN_PYTHON:-$ROOT_DIR/venv/bin/python}"

# faster-whisper/CTranslate2 4.4 的 CUDA 语音识别路径需要 cuDNN 8。
# 与 VoxCPM 使用的 cuDNN 9 分离，避免两个虚拟环境互相覆盖动态库。
ASR_CUDNN8_LIB_DIR="${ASR_CUDNN8_LIB_DIR:-$ROOT_DIR/asr-cudnn8/nvidia/cudnn/lib}"
ASR_CTRANSLATE2_LIB_DIR="$ROOT_DIR/venv/lib/python3.12/site-packages/ctranslate2.libs"
ASR_CUBLAS_LIB_DIR="$ROOT_DIR/venv/lib/python3.12/site-packages/nvidia/cublas/lib"
if [[ ! -f "$ASR_CUDNN8_LIB_DIR/libcudnn_ops_infer.so.8" ]]; then
  echo "ERROR: ASR cuDNN 8 library is missing: $ASR_CUDNN8_LIB_DIR/libcudnn_ops_infer.so.8" >&2
  echo "       Install the dedicated ASR cuDNN 8 runtime before starting 7860." >&2
  exit 4
fi
ASR_LD_LIBRARY_PATH="$ASR_CUDNN8_LIB_DIR:$ASR_CTRANSLATE2_LIB_DIR:$ASR_CUBLAS_LIB_DIR"

# WSL2 地址会在重启后变化，更新 Windows 侧本地端口转发。
WSL_IP="$(hostname -I | awk '{print $1}')"
NETSH_EXE="/mnt/c/Windows/System32/netsh.exe"
if [[ -x "$NETSH_EXE" && -n "$WSL_IP" ]]; then
  for PORT in 7860 8010 8011; do
    "$NETSH_EXE" interface portproxy set v4tov4 \
      listenaddress=127.0.0.1 listenport="$PORT" \
      connectaddress="$WSL_IP" connectport="$PORT" >/dev/null 2>&1 || true
  done
fi

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
LIVETALKING_LIFECYCLE_PATCH="$ROOT_DIR/scripts/livetalking-session-lifecycle.patch"
LIVETALKING_RTC_MANAGER="$ROOT_DIR/LiveTalking/server/rtc_manager.py"

if [[ ! -f "$LIVETALKING_LIFECYCLE_PATCH" ]]; then
  echo "ERROR: LiveTalking session lifecycle patch is missing: $LIVETALKING_LIFECYCLE_PATCH" >&2
  exit 3
fi

if ! grep -Fq "expected_session=avatar_session" "$LIVETALKING_RTC_MANAGER"; then
  echo "=== 应用 LiveTalking 会话生命周期补丁 ==="
  patch --batch -p1 -d "$ROOT_DIR" < "$LIVETALKING_LIFECYCLE_PATCH"
fi

if grep -q "Clocked-v2" "$ROOT_DIR/LiveTalking/server/routes.py"; then
  echo "=== 升级 LiveTalking 会话重绑定补丁 ==="
  patch --batch -p1 -d "$ROOT_DIR" \
    < "$ROOT_DIR/scripts/livetalking-stream-v2-to-v3.patch"
fi
if grep -q "async def humanaudio_stream" "$ROOT_DIR/LiveTalking/server/routes.py" && \
   ! grep -q "Clocked-v3" "$ROOT_DIR/LiveTalking/server/routes.py"; then
  echo "=== 升级 LiveTalking 固定时钟音频流补丁 ==="
  patch --batch -R -p1 -d "$ROOT_DIR" \
    < "$ROOT_DIR/scripts/livetalking-stream-v1.patch"
fi
if [[ -f "$ROOT_DIR/scripts/livetalking-stream.patch" ]] && \
   ! grep -q "Clocked-v3" "$ROOT_DIR/LiveTalking/server/routes.py"; then
  echo "=== 应用 LiveTalking 持续音频流补丁 ==="
  patch --batch -p1 -d "$ROOT_DIR" \
    < "$ROOT_DIR/scripts/livetalking-stream.patch"
fi
cd "$ROOT_DIR/LiveTalking"
nohup "$MAIN_PYTHON" app.py --transport webrtc --model wav2lip --avatar_id wav2lip256_avatar_256 --listenport 8010 > /tmp/livetalking.log 2>&1 &
echo "  PID=$!"

echo "=== 启动 avatar-sync (8011) ==="
cd "$ROOT_DIR"
setsid -f node "$ROOT_DIR/scripts/avatar-sync.js" > /tmp/avatar-sync.log 2>&1 < /dev/null &
echo "  PID=$!"

echo "=== 启动 VoxCPM Worker (8020) ==="
cd "$ROOT_DIR"
nohup bash "$ROOT_DIR/scripts/start-voxcpm-worker.sh" > /tmp/voxcpm-worker.log 2>&1 &
echo "  PID=$!"

echo "=== 启动 app.py (7860) ==="
cd "$ROOT_DIR"
nohup env LD_LIBRARY_PATH="$ASR_LD_LIBRARY_PATH${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
  "$MAIN_PYTHON" "$ROOT_DIR/outputs/backend/app.py" > /tmp/app.log 2>&1 &
echo "  PID=$!"

sleep 5

echo ""
echo "=== 服务状态 ==="
ss -tlnp | grep -E "7860|8010|8011|8020" || echo "有服务未启动，请检查日志:"
echo "  LiveTalking:  tail /tmp/livetalking.log"
echo "  avatar-sync:  tail /tmp/avatar-sync.log"
echo "  VoxCPM Worker: tail /tmp/voxcpm-worker.log"
echo "  app.py:       tail /tmp/app.log"
