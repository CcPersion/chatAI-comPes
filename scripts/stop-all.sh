#!/usr/bin/env bash
# =============================================================================
# stop-all.sh — WSL2 侧停止所有服务
# =============================================================================
# 终止 Gradio(:7860)、LiveTalking(:8010)、avatar-sync(:8011) 进程，
# 清理残留端口。
# 运行: bash stop-all.sh
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

# ---- 优雅停止：先读 PID 文件 ----
stop_by_pidfile() {
    local pidfile=$1
    local name=$2
    if [ -f "$pidfile" ]; then
        local pid=$(cat "$pidfile" 2>/dev/null || echo "")
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            log_info "正在终止 ${name} (PID: $pid)..."
            kill "$pid" 2>/dev/null || true
            sleep 1
            # 如果仍在运行，强制终止
            if kill -0 "$pid" 2>/dev/null; then
                log_warn "${name} 未响应 SIGTERM，强制终止..."
                kill -9 "$pid" 2>/dev/null || true
            fi
            log_info "${name} 已终止。"
        else
            log_info "${name} PID 文件存在但进程不存在，跳过。"
        fi
        rm -f "$pidfile"
    fi
}

stop_by_pidfile "${SETUP_DIR}/voice.pid" "Gradio (voice)"
stop_by_pidfile "${SETUP_DIR}/livetalking.pid" "LiveTalking"
stop_by_pidfile "${SETUP_DIR}/avatar-sync.pid" "avatar-sync"

# ---- 按端口强制清理 ----
kill_port_processes() {
    local port=$1
    local name=$2
    local pids=$(ss -tlnp 2>/dev/null | grep ":${port} " | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | sort -u || true)

    if [ -z "$pids" ]; then
        log_info "端口 ${port} (${name}) 无占用。"
        return
    fi

    for pid in $pids; do
        log_info "端口 ${port} 被 PID ${pid} 占用，正在终止..."
        kill "$pid" 2>/dev/null || true
        sleep 0.5
        if kill -0 "$pid" 2>/dev/null; then
            log_warn "PID ${pid} 未响应，强制终止..."
            kill -9 "$pid" 2>/dev/null || true
        fi
        log_info "PID ${pid} 已终止。"
    done
}

log_info "正在停止 WSL2 侧所有服务..."

kill_port_processes 7860 "Gradio speech-to-speech"
kill_port_processes 8010 "LiveTalking"
kill_port_processes 8011 "avatar-sync"

# ---- 最终确认 ----
echo ""
log_info "端口检查:"
for port in 7860 8010 8011; do
    if ss -tlnp 2>/dev/null | grep -q ":${port} "; then
        log_warn "端口 ${port} 仍有残留占用！"
    else
        log_info "端口 ${port} 已释放。"
    fi
done

log_info "WSL2 侧服务已全部停止。"
echo ""
log_info "提示: 还需在 Windows 侧运行 stop-all.bat 停止 llama-server。"
