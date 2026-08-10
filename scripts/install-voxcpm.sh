#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${VOXCPM_VENV:-$ROOT_DIR/.venv-voxcpm}"
PYTHON_BIN="${VOXCPM_PYTHON:-python3.11}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "ERROR: Python 3.11 was not found. Set VOXCPM_PYTHON to Python 3.10-3.12." >&2
  exit 2
fi

"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$ROOT_DIR/outputs/backend/requirements-voxcpm.txt"

echo "VoxCPM worker environment installed at $VENV_DIR"
echo "Model weights are intentionally not downloaded by this script."
