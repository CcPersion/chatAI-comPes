#!/usr/bin/env bash
# =============================================================================
# start-livetalking.sh — WSL2 侧启动 LiveTalking 服务
# =============================================================================
# 用途: 启动 LiveTalking Python 服务（:8010），加载 wav2lip256.pth + idle.mp4。
# 运行: bash start-livetalking.sh
# 前置条件: install-voice.sh 已完成，wav2lip256.pth 与 idle.mp4 位于 ~/setup/。
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
LIVETALKING_LOG="${LOG_DIR}/livetalking.log"

# ---- 目录准备 ----
mkdir -p "${LOG_DIR}"

# ---- 前置检查 ----
if [ ! -f "${SETUP_DIR}/wav2lip256.pth" ]; then
    log_error "找不到 wav2lip256.pth，请确认文件位于: ${SETUP_DIR}/wav2lip256.pth"
    exit 1
fi

if [ ! -f "${SETUP_DIR}/idle.mp4" ]; then
    log_error "找不到 idle.mp4（数字人底版），请确认文件位于: ${SETUP_DIR}/idle.mp4"
    exit 1
fi

# ---- 激活虚拟环境 ----
if [ -f "${VENV_DIR}/bin/activate" ]; then
    source "${VENV_DIR}/bin/activate"
else
    log_error "Python 虚拟环境不存在，请先运行 install-voice.sh"
    exit 1
fi

# ---- 启动 LiveTalking ----
log_info "正在启动 LiveTalking..."
log_info "  模型: ${SETUP_DIR}/wav2lip256.pth"
log_info "  底版: ${SETUP_DIR}/idle.mp4"
log_info "  监听: 127.0.0.1:8010"
log_info "  日志: ${LIVETALKING_LOG}"

# LiveTalking 启动命令
# 说明: LiveTalking 的具体启动方式取决于其实现（零度教程方案）。
# 此处提供通用模板，实际参数按 LiveTalking 项目的入口文件调整。
# 常见入口文件: app.py, main.py, livetalking_server.py 等。
#
# 若 LiveTalking 使用命令行参数：
# python livetalking_server.py \
#     --host 127.0.0.1 \
#     --port 8010 \
#     --wav2lip-model "${SETUP_DIR}/wav2lip256.pth" \
#     --idle-video "${SETUP_DIR}/idle.mp4" \
#     > "${LIVETALKING_LOG}" 2>&1 &
#
# 若 LiveTalking 是模块方式导入，则通过 app.py 统一启动（见 outputs/backend/app.py）。

# 这里使用独立进程方式启动 LiveTalking
# 实际项目中，LiveTalking 由 start-voice.sh 中嵌入的 avatar-sync 链路驱动，
# 本脚本作为独立调试/冷备启动用。
#
# 以下为 LiveTalking 的独立启动模板（按实际入口调整）：
LIVETALKING_ENTRY=""
for candidate in \
    "${SETUP_DIR}/livetalking_server.py" \
    "${SETUP_DIR}/app.py" \
    "/opt/LiveTalking/server.py"; do
    if [ -f "$candidate" ]; then
        LIVETALKING_ENTRY="$candidate"
        break
    fi
done

if [ -z "$LIVETALKING_ENTRY" ]; then
    log_warn "未找到 LiveTalking 独立入口文件。"
    log_warn "LiveTalking 将在 start-voice.sh 中集成启动，本脚本仅做探活兜底。"
    log_info "若 LiveTalking 已通过其他方式启动，检测到端口 8010 响应即可。"
else
    log_info "找到 LiveTalking 入口: ${LIVETALKING_ENTRY}"
    nohup python "${LIVETALKING_ENTRY}" \
        --host 127.0.0.1 \
        --port 8010 \
        --wav2lip-model "${SETUP_DIR}/wav2lip256.pth" \
        --idle-video "${SETUP_DIR}/idle.mp4" \
        > "${LIVETALKING_LOG}" 2>&1 &
    LIVE_PID=$!
    log_info "LiveTalking PID: ${LIVE_PID}"
fi

# ---- 轮询等待就绪 ----
log_info "正在等待 LiveTalking 就绪 (127.0.0.1:8010)..."

MAX_RETRIES=60
RETRY=0

while [ $RETRY -lt $MAX_RETRIES ]; do
    # The bundled LiveTalking server does not expose /health. Its embedded
    # avatar page is the stable readiness probe used by the web client.
    if curl -s -o /dev/null -w '%{http_code}' --connect-timeout 2 "http://127.0.0.1:8010/avatar-embed.html" 2>/dev/null | grep -q '200'; then
        log_info "LiveTalking 就绪! 监听 http://127.0.0.1:8010"
        exit 0
    fi
    RETRY=$((RETRY + 1))
    sleep 1
done

log_error "LiveTalking 启动超时（已等待 ${MAX_RETRIES} 秒）。"
log_error "请检查日志: ${LIVETALKING_LOG}"
exit 1
