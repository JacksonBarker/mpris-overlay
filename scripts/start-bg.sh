#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
PID_FILE="${MPRIS_OVERLAY_PID_FILE:-$ROOT_DIR/overlay.pid}"
LOG_FILE="${MPRIS_OVERLAY_LOG_FILE:-$ROOT_DIR/overlay.log}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Missing virtual environment Python at $PYTHON_BIN"
  echo "Create it first: python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi

if [[ -f "$PID_FILE" ]]; then
  EXISTING_PID="$(cat "$PID_FILE")"
  if [[ -n "${EXISTING_PID}" ]] && kill -0 "$EXISTING_PID" >/dev/null 2>&1; then
    echo "Overlay is already running (PID $EXISTING_PID)"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

nohup "$PYTHON_BIN" "$ROOT_DIR/overlay_server.py" \
  --host 127.0.0.1 \
  --port 8765 \
  --dbus-mode auto \
  "$@" >"$LOG_FILE" 2>&1 &

PID="$!"
echo "$PID" >"$PID_FILE"
echo "Started overlay (PID $PID)"
echo "Log: $LOG_FILE"
