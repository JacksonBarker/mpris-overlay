#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="${MPRIS_OVERLAY_PID_FILE:-$ROOT_DIR/overlay.pid}"

if [[ ! -f "$PID_FILE" ]]; then
  echo "No PID file found at $PID_FILE (overlay may not be running)."
  exit 0
fi

PID="$(cat "$PID_FILE")"
if [[ -z "${PID}" ]]; then
  rm -f "$PID_FILE"
  echo "PID file was empty. Removed stale file."
  exit 0
fi

if kill -0 "$PID" >/dev/null 2>&1; then
  kill "$PID"
  echo "Stopped overlay (PID $PID)"
else
  echo "Process $PID is not running. Removing stale PID file."
fi

rm -f "$PID_FILE"
