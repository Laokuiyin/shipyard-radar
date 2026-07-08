#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${SHIPWATCH_PROJECT_DIR:-/opt/shipyard-radar-main}"
cd "$PROJECT_DIR"

mkdir -p data
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

"$PROJECT_DIR/venv/bin/python" -m shipwatch.cli daily >> data/daily.log 2>&1
