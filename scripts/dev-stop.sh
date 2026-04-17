#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_DIR="$ROOT_DIR/.run"

ok()   { printf '\033[0;32m✅ %s\033[0m\n' "$*"; }
info() { printf '\033[0;36m%s\033[0m\n' "$*"; }

if [ ! -d "$RUN_DIR" ]; then
  ok "No services running."
  exit 0
fi

STOPPED=0
for pidfile in "$RUN_DIR"/*.pid; do
  [ -f "$pidfile" ] || continue
  name=$(basename "$pidfile" .pid)
  pid=$(cat "$pidfile")
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    info "Stopped $name (PID $pid)"
    STOPPED=$((STOPPED + 1))
  else
    info "$name (PID $pid) was already stopped"
  fi
done

rm -rf "$RUN_DIR"

if [ "$STOPPED" -gt 0 ]; then
  ok "All services stopped."
else
  ok "No running services found."
fi
