#!/usr/bin/env bash
# =============================================================================
# start-voice.sh — WSL2 侧启动 speech-to-speech 管线 (Gradio :7860)
# =============================================================================
# 用途: 启动 Gradio 应用（:7860），加载 VAD/faster-whisper/Qwen3-TTS 配置。
# 运行: bash start-voice.sh
# 前置条件: install-voice.sh 已完成，llama-server (Windows :8090) 已就绪。
# =============================================================================
set -euo pipefail

# ---- 颜色输出 ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

SETUP_DIR="${HOME}/setup"
VENV_DIR="${SETUP_DIR}/venv"
LOG_DIR="${SETUP_DIR}/logs"
VOICE_LOG="${LOG_DIR}/voice.log"
CONFIG_FILE="${SETUP_DIR}/voice.yaml"

# ---- 目录准备 ----
mkdir -p "${LOG_DIR}"

# ---- 前置检查 ----
if [ ! -f "${CONFIG_FILE}" ]; then
    log_error "找不到配置文件: ${CONFIG_FILE}"
    log_info "请从项目仓库复制 config/voice.yaml 到 ${SETUP_DIR}/"
    exit 1
fi

if [ ! -f "${VENV_DIR}/bin/activate" ]; then
    log_error "Python 虚拟环境不存在，请先运行 install-voice.sh"
    exit 1
fi

# ---- 激活虚拟环境 ----
source "${VENV_DIR}/bin/activate"

# ---- 读取配置 ----
log_info "加载配置: ${CONFIG_FILE}"

# ---- 启动 Gradio 应用 ----
log_info "正在启动 speech-to-speech 管线..."

# 确定 app.py 位置
APP_PY=""
for candidate in \
    "${SETUP_DIR}/app.py" \
    "$(dirname "$0")/../outputs/backend/app.py" \
    "/opt/voice-pipeline/app.py"; do
    if [ -f "$candidate" ]; then
        APP_PY="$candidate"
        break
    fi
done

if [ -z "$APP_PY" ]; then
    log_error "找不到 app.py！请确认 outputs/backend/app.py 已复制到 WSL2。"
    log_info "手动复制: cp outputs/backend/app.py ${SETUP_DIR}/"
    exit 1
fi

log_info "入口文件: ${APP_PY}"
log_info "监听: 127.0.0.1:7860 (仅本地访问，WSL2 localhost 转发对 Windows 可见)"
log_info "日志: ${VOICE_LOG}"

# 启动 Gradio（后台运行，日志写入文件）
# Gradio 默认监听 0.0.0.0:7860，通过 WSL2 localhostForwarding 对 Windows 可见。
# 若需仅监听 127.0.0.1，可在 app.py 中设置 server_name="127.0.0.1"。
nohup python "${APP_PY}" > "${VOICE_LOG}" 2>&1 &
APP_PID=$!
echo "${APP_PID}" > "${SETUP_DIR}/voice.pid"
log_info "Gradio PID: ${APP_PID}"

# ---- 轮询等待就绪 ----
log_info "正在等待 Gradio 页面就绪 (http://127.0.0.1:7860)..."

MAX_RETRIES=90
RETRY=0

while [ $RETRY -lt $MAX_RETRIES ]; do
    if curl -s -o /dev/null -w '%{http_code}' --connect-timeout 2 "http://127.0.0.1:7860/" 2>/dev/null | grep -q '200'; then
        log_info "speech-to-speech 管线就绪! http://localhost:7860"
        log_info "浏览器打开: http://localhost:7860"
        exit 0
    fi
    RETRY=$((RETRY + 1))
    sleep 1
done

log_error "Gradio 启动超时（已等待 ${MAX_RETRIES} 秒）。"
log_error "请检查日志: ${VOICE_LOG}"
exit 1
