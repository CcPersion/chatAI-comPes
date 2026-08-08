#!/usr/bin/env bash
# =============================================================================
# install-voice.sh — 首次安装脚本（WSL2 侧）
# =============================================================================
# 用途: 创建 Python venv、安装依赖、下载模型、校验哈希、环境检查。
# 运行: bash install-voice.sh
# 前置条件: WSL2 Ubuntu 24.04，CUDA 与 GPU 透传已配置。
# =============================================================================
set -euo pipefail

# ---- 颜色输出 ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ---- 路径配置 ----
SETUP_DIR="$HOME/setup"
VENV_DIR="$SETUP_DIR/venv"
MODELS_DIR="$SETUP_DIR/models"
LOGS_DIR="$SETUP_DIR/logs"
UPLOADS_DIR="$SETUP_DIR/uploads"
ASSETS_SRC="$(dirname "$0")/../assets"

echo "============================================================"
echo "  local-ai-companion-v2 首次安装"
echo "  目标目录: $SETUP_DIR"
echo "============================================================"

# =========================================================================
# Step 1: 环境检查
# =========================================================================
log_info "Step 1/7: 环境检查..."

# 检查 CUDA 是否可用
if command -v nvidia-smi &>/dev/null; then
    GPU_MEM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 || echo "0")
    log_info "CUDA 可用，GPU 总显存: ${GPU_MEM} MB"
    if [ "$GPU_MEM" -lt 16000 ] 2>/dev/null; then
        log_warn "显存不足 16GB（当前 $GPU_MEM MB），16GB 卡为最低要求。"
        log_warn "可能无法同时加载所有模型，建议使用 Qwen3-8B。"
    fi
else
    log_error "nvidia-smi 不可用！请确认："
    log_error "  1. NVIDIA 驱动已安装"
    log_error "  2. WSL2 GPU 透传已配置"
    log_error "  3. CUDA 工具包已安装"
    exit 1
fi

# 检查端口占用
check_port() {
    local port=$1
    if ss -tlnp 2>/dev/null | grep -q ":$port "; then
        log_warn "端口 $port 已被占用，安装后可继续但启动时需先释放。"
    fi
}
check_port 7860
check_port 8010
check_port 8011

log_info "环境检查通过。"

# =========================================================================
# Step 2: 创建目录结构
# =========================================================================
log_info "Step 2/7: 创建目录结构..."
mkdir -p "$SETUP_DIR" "$VENV_DIR" "$MODELS_DIR" "$LOGS_DIR" "$UPLOADS_DIR"
mkdir -p "$MODELS_DIR/faster-whisper-large-v3"
mkdir -p "$MODELS_DIR/Qwen3-TTS-1.7B"
log_info "目录结构已创建: $SETUP_DIR"

# =========================================================================
# Step 3: 创建 Python 虚拟环境
# =========================================================================
log_info "Step 3/7: 创建 Python 虚拟环境..."
if [ -d "$VENV_DIR/bin" ]; then
    log_warn "虚拟环境已存在，跳过创建。若需重新创建，请先删除 $VENV_DIR。"
else
    python3 -m venv "$VENV_DIR"
    log_info "虚拟环境已创建: $VENV_DIR"
fi

# 激活虚拟环境
source "$VENV_DIR/bin/activate"

# 升级 pip
log_info "升级 pip..."
pip install --upgrade pip setuptools wheel -q

# =========================================================================
# Step 4: 安装 Python 依赖
# =========================================================================
log_info "Step 4/7: 安装 Python 依赖..."
# 依赖列表来自 outputs/backend/requirements.txt
REQUIREMENTS_FILE="$(dirname "$0")/../outputs/backend/requirements.txt"
if [ -f "$REQUIREMENTS_FILE" ]; then
    pip install -r "$REQUIREMENTS_FILE"
    log_info "Python 依赖安装完成。"
else
    log_error "找不到 requirements.txt: $REQUIREMENTS_FILE"
    log_error "请从项目仓库复制 outputs/backend/requirements.txt 后再运行。"
    exit 1
fi

# =========================================================================
# Step 5: 下载模型文件
# =========================================================================
log_info "Step 5/7: 下载模型文件..."

# 5a. faster-whisper large-v3
WHISPER_DIR="$MODELS_DIR/faster-whisper-large-v3"
if [ -d "$WHISPER_DIR" ] && [ "$(ls -A "$WHISPER_DIR" 2>/dev/null)" ]; then
    log_warn "faster-whisper-large-v3 模型目录非空，跳过下载。"
    log_warn "若需重新下载，请先删除: $WHISPER_DIR"
else
    log_info "正在下载 faster-whisper-large-v3（首次下载约 3GB，请耐心等待）..."
    python3 -c "
from faster_whisper import download_model
download_model('large-v3', output_dir='$WHISPER_DIR')
print('faster-whisper-large-v3 下载完成。')
" || {
        log_error "faster-whisper-large-v3 下载失败，请检查网络连接。"
        log_error "手动下载: huggingface-cli download Systran/faster-whisper-large-v3 --local-dir $WHISPER_DIR"
        exit 1
    }
    log_info "faster-whisper-large-v3 下载完成。"
fi

# 5b. Qwen3-TTS-1.7B
TTS_DIR="$MODELS_DIR/Qwen3-TTS-1.7B"
if [ -d "$TTS_DIR" ] && [ "$(ls -A "$TTS_DIR" 2>/dev/null)" ]; then
    log_warn "Qwen3-TTS-1.7B 模型目录非空，跳过下载。"
    log_warn "若需重新下载，请先删除: $TTS_DIR"
else
    log_info "正在下载 Qwen3-TTS-1.7B（首次下载约 4GB，请耐心等待）..."
    # 使用 huggingface_hub 下载模型
    python3 -c "
from huggingface_hub import snapshot_download
snapshot_download('Qwen/Qwen3-TTS-1.7B', local_dir='$TTS_DIR')
print('Qwen3-TTS-1.7B 下载完成。')
" || {
        log_error "Qwen3-TTS-1.7B 下载失败，请检查网络连接。"
        log_error "手动下载: huggingface-cli download Qwen/Qwen3-TTS-1.7B --local-dir $TTS_DIR"
        exit 1
    }
    log_info "Qwen3-TTS-1.7B 下载完成。"
fi

# =========================================================================
# Step 6: 复制 assets/ 到 ~/setup/
# =========================================================================
log_info "Step 6/7: 复制运行时资产..."
if [ -d "$ASSETS_SRC" ]; then
    if [ -f "$ASSETS_SRC/wav2lip256.pth" ]; then
        cp "$ASSETS_SRC/wav2lip256.pth" "$SETUP_DIR/"
        log_info "已复制 wav2lip256.pth"
    else
        log_warn "未找到 assets/wav2lip256.pth，请手动下载后放到 $SETUP_DIR/"
    fi
    if [ -f "$ASSETS_SRC/idle.mp4" ]; then
        cp "$ASSETS_SRC/idle.mp4" "$SETUP_DIR/"
        log_info "已复制 idle.mp4"
    else
        log_warn "未找到 assets/idle.mp4，请用 ComfyUI+Wan2.2 生成后放到 $SETUP_DIR/"
    fi
    if [ -f "$ASSETS_SRC/ref.wav" ]; then
        cp "$ASSETS_SRC/ref.wav" "$SETUP_DIR/"
        log_info "已复制 ref.wav"
    else
        log_warn "未找到 assets/ref.wav，请在首次运行时上传参考音频或手动放入。"
    fi
else
    log_warn "未找到 assets/ 目录，跳过资产复制。"
    log_warn "wav2lip256.pth 和 idle.mp4 需要手动放入 $SETUP_DIR/"
fi

# 复制脚本到 setup 目录
SCRIPT_DIR="$(dirname "$0")"
for script in start-voice.sh start-livetalking.sh avatar-sync.js; do
    if [ -f "$SCRIPT_DIR/$script" ]; then
        cp "$SCRIPT_DIR/$script" "$SETUP_DIR/"
        log_info "已复制 $script"
    fi
done

# 复制配置文件
if [ -f "$SCRIPT_DIR/../config/voice.yaml" ]; then
    cp "$SCRIPT_DIR/../config/voice.yaml" "$SETUP_DIR/"
    log_info "已复制 voice.yaml"
fi

# =========================================================================
# Step 7: 校验关键文件
# =========================================================================
log_info "Step 7/7: 校验关键文件..."

check_file() {
    local path=$1
    local desc=$2
    if [ -f "$path" ]; then
        local size=$(du -h "$path" 2>/dev/null | cut -f1)
        local hash=$(sha256sum "$path" 2>/dev/null | cut -d' ' -f1 || echo "N/A")
        log_info "  [OK] $desc: $path ($size, SHA256: $hash)"
    else
        log_warn "  [MISS] $desc: $path (不存在)"
    fi
}

check_file "$SETUP_DIR/wav2lip256.pth" "wav2lip256 权重"
check_file "$SETUP_DIR/idle.mp4" "数字人底版视频"
check_file "$SETUP_DIR/ref.wav" "音色克隆参考音频"
check_file "$WHISPER_DIR/config.json" "faster-whisper 配置"
check_file "$TTS_DIR/config.json" "Qwen3-TTS 配置"

echo ""
echo "============================================================"
log_info "安装完成！"
echo ""
echo "  后续步骤："
echo "  1. Windows 侧运行: start-llama.bat"
echo "  2. WSL2 侧运行:  bash $SETUP_DIR/start-livetalking.sh"
echo "  3. WSL2 侧运行:  bash $SETUP_DIR/start-voice.sh"
echo "  4. 浏览器打开:   http://localhost:7860"
echo "============================================================"
