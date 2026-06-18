#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${SHIPWATCH_PROJECT_DIR:-/opt/shipwatch}"
cd "$PROJECT_DIR"

mkdir -p logs
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

"$PROJECT_DIR/.venv/bin/shipwatch" daily >> logs/daily.log 2>&1

