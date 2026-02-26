#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Missing virtual environment Python at $PYTHON_BIN"
  echo "Create it first: python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi

exec "$PYTHON_BIN" "$ROOT_DIR/overlay_server.py" \
  --host 127.0.0.1 \
  --port 8765 \
  --dbus-mode auto \
  "$@"
