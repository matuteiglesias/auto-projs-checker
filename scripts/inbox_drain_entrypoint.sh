#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${1:-$REPO_DIR/private/inbox_runtime.env}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

: "${PDF_PROCESSOR_CMD:?PDF_PROCESSOR_CMD is required (set in env file or environment)}"

cd "$REPO_DIR"
mkdir -p data/archive/logs

echo "[$(date -u +%FT%TZ)] inbox drain start"
make drain-inbox
echo "[$(date -u +%FT%TZ)] inbox drain end"
