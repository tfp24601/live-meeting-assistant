#!/usr/bin/env bash
# Launch the LiveMeetingAssistant backend on Sol.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Load config if present
if [[ -f "lma.env" ]]; then
  set -a; source lma.env; set +a
fi

VENV="$REPO_ROOT/.venv"
if [[ ! -x "$VENV/bin/uvicorn" ]]; then
  echo "venv not found or incomplete at $VENV — run deploy/setup-venv.sh first" >&2
  exit 1
fi

# With the claude-cli provider, strip any API key so the CLI bills to the
# subscription (an inherited key also switches it to an old request schema).
# Other providers (anthropic-api) legitimately need the key — leave it alone.
if [[ "${LLM_PROVIDER:-claude-cli}" == "claude-cli" ]]; then
  unset ANTHROPIC_API_KEY || true
fi

# faster-whisper (CTranslate2) loads cuBLAS/cuDNN from the pip-installed nvidia
# wheels in the venv; make those discoverable at runtime.
if [[ "${WHISPER_DEVICE:-cuda}" == "cuda" ]]; then
  NVIDIA_LIBS="$(find "$VENV"/lib/python*/site-packages/nvidia -maxdepth 2 -name lib -type d 2>/dev/null | paste -sd: -)"
  if [[ -n "$NVIDIA_LIBS" ]]; then
    export LD_LIBRARY_PATH="${NVIDIA_LIBS}:${LD_LIBRARY_PATH:-}"
  fi
fi

exec "$VENV/bin/uvicorn" app.main:app \
  --app-dir "$REPO_ROOT/backend" \
  --host "${LMA_HOST:-0.0.0.0}" \
  --port "${LMA_PORT:-5005}"
