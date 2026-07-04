#!/usr/bin/env bash
# Push the source tree to the machine that runs the backend.
#
# Usage:  ./deploy/deploy-remote.sh user@host /remote/path/to/LiveMeetingAssistant/
#    or:  LMA_REMOTE=user@host LMA_REMOTE_PATH=/path ./deploy/deploy-remote.sh
#
# Deployment-local files (lma.env, settings.json, sources.yaml, data/, models)
# are never touched.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/"
REMOTE="${1:-${LMA_REMOTE:-}}"
DEST="${2:-${LMA_REMOTE_PATH:-}}"
if [[ -z "$REMOTE" || -z "$DEST" ]]; then
  echo "usage: $0 user@host /remote/path/   (or set LMA_REMOTE + LMA_REMOTE_PATH)" >&2
  exit 1
fi

echo "Deploying $SRC -> $REMOTE:$DEST"
ssh "$REMOTE" "mkdir -p '$DEST'"
rsync -az --delete \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude '.venv-*/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude 'backend/models/' \
  --exclude 'data/' \
  --exclude 'lma.env' \
  --exclude 'sources.yaml' \
  --exclude 'settings.json' \
  --exclude '*.local.sh' \
  "$SRC" "$REMOTE:$DEST"
echo "Done. First time on the remote: ./deploy/setup-venv.sh, copy backend/app/.env.example"
echo "to lma.env and sources.example.yaml to sources.yaml, then ./backend/run.sh"
