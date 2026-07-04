#!/usr/bin/env bash
# One-time (idempotent) venv setup. Run this on Sol, in the repo root.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
VENV="$REPO_ROOT/.venv"

if [[ ! -d "$VENV" ]]; then
  echo "Creating venv at $VENV"
  python3 -m venv "$VENV"
fi

"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -r backend/requirements.txt

# faster-whisper uses CTranslate2, which needs CUDA 12 cuBLAS + cuDNN 9 at runtime.
# Install them as pip wheels into the venv so we don't depend on system CUDA libs.
if [[ "${WHISPER_DEVICE:-cuda}" == "cuda" ]]; then
  echo "Installing CUDA runtime libs (cuBLAS + cuDNN 9) for GPU inference"
  "$VENV/bin/pip" install "nvidia-cublas-cu12" "nvidia-cudnn-cu12>=9,<10"
fi

echo "venv ready. Start with: ./backend/run.sh"
