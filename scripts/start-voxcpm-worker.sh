#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VOICE_CONFIG="${VOICE_CONFIG:-$ROOT_DIR/config/voice.yaml}"
VENV_DIR="${VOXCPM_VENV:-$ROOT_DIR/.venv-voxcpm}"
PYTHON_BIN="$VENV_DIR/bin/python"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "ERROR: dedicated VoxCPM venv is missing: $VENV_DIR" >&2
  echo "Create it and install the local VoxCPM dependencies before starting; no system Python fallback is allowed." >&2
  exit 2
fi
if [[ ! -f "$VOICE_CONFIG" ]]; then echo "ERROR: voice config not found: $VOICE_CONFIG" >&2; exit 2; fi

read_config() { "$PYTHON_BIN" - "$VOICE_CONFIG" "$1" <<'PY'
import sys, yaml
data = yaml.safe_load(open(sys.argv[1], encoding='utf-8')) or {}
value = data.get(sys.argv[2], '')
print('' if value is None else value)
PY
}

MODEL_ID="$(read_config VOXCPM_MODEL_ID)"
PROFILE="$(read_config VOXCPM_PROFILE)"
MODEL_PATH="$(read_config VOXCPM_MODEL_PATH)"
REF_WAV="$(read_config VOXCPM_REF_WAV)"
REF_TEXT="$(read_config VOXCPM_REF_TEXT)"
REFERENCE_ROOT="$(read_config UPLOAD_DIR)"
SAMPLE_RATE="$(read_config VOXCPM_SAMPLE_RATE)"
WORKER_URL="$(read_config VOXCPM_WORKER_URL)"
INFERENCE_TIMESTEPS="$(read_config VOXCPM_INFERENCE_TIMESTEPS)"
INFERENCE_TIMESTEPS="${INFERENCE_TIMESTEPS:-4}"

# Bash does not expand a tilde when it arrives through a variable.
MODEL_PATH="${MODEL_PATH/#\~/$HOME}"
REF_WAV="${REF_WAV/#\~/$HOME}"
REFERENCE_ROOT="${REFERENCE_ROOT/#\~/$HOME}"

[[ "$MODEL_ID" == "VoxCPM2" || "$MODEL_ID" == "VoxCPM1.5" ]] || { echo "ERROR: VOXCPM_MODEL_ID must be VoxCPM2 or VoxCPM1.5, got: $MODEL_ID" >&2; exit 2; }
[[ "$PROFILE" == "balanced-v2" || "$PROFILE" == "safe-v15" ]] || { echo "ERROR: unsupported VOXCPM_PROFILE: $PROFILE" >&2; exit 2; }
[[ "$MODEL_PATH" = /* ]] || { echo "ERROR: model path must be absolute after config expansion: $MODEL_PATH" >&2; exit 2; }
[[ -d "$MODEL_PATH" ]] || { echo "ERROR: local VoxCPM model directory not found: $MODEL_PATH" >&2; exit 2; }
[[ "$PYTHON_BIN" == *"/python" ]] || { echo "ERROR: invalid dedicated Python path: $PYTHON_BIN" >&2; exit 2; }
PY_VERSION="$($PYTHON_BIN -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
[[ "$PY_VERSION" == 3.10 || "$PY_VERSION" == 3.11 || "$PY_VERSION" == 3.12 ]] || { echo "ERROR: VoxCPM requires Python 3.10-3.12, found $PY_VERSION" >&2; exit 2; }
[[ "$WORKER_URL" =~ ^http://127\.0\.0\.1:([0-9]+)$ ]] || { echo "ERROR: VOXCPM_WORKER_URL must be loopback http://127.0.0.1:PORT, got: $WORKER_URL" >&2; exit 2; }
PORT="${BASH_REMATCH[1]}"
[[ "$MODEL_ID" == "VoxCPM2" && "$SAMPLE_RATE" == 48000 || "$MODEL_ID" == "VoxCPM1.5" && "$SAMPLE_RATE" == 44100 ]] || { echo "ERROR: configured sample rate does not match model: $MODEL_ID / $SAMPLE_RATE" >&2; exit 2; }
[[ -f "$ROOT_DIR/outputs/backend/voxcpm_worker.py" ]] || { echo "ERROR: worker source missing" >&2; exit 2; }

exec "$PYTHON_BIN" "$ROOT_DIR/outputs/backend/voxcpm_worker.py" --host 127.0.0.1 --port "$PORT" --model-path "$MODEL_PATH" --model-id "$MODEL_ID" --profile "$PROFILE" --ref-wav "$REF_WAV" --ref-text "$REF_TEXT" --reference-root "$REFERENCE_ROOT" --sample-rate "$SAMPLE_RATE" --inference-timesteps "$INFERENCE_TIMESTEPS"
