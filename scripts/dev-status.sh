#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_DIR="$ROOT_DIR/.run"

ok()   { printf '\033[0;32m✅ %s\033[0m\n' "$*"; }
warn() { printf '\033[0;33m⚠️  %s\033[0m\n' "$*"; }
info() { printf '\033[0;36m%s\033[0m\n' "$*"; }

if [ ! -d "$RUN_DIR" ]; then
  info "No services registered."
  exit 0
fi

echo ""
for pidfile in "$RUN_DIR"/*.pid; do
  [ -f "$pidfile" ] || continue
  name=$(basename "$pidfile" .pid)
  pid=$(cat "$pidfile")
  if kill -0 "$pid" 2>/dev/null; then
    ok "$name is running (PID $pid)"
  else
    warn "$name is NOT running (PID $pid exited)"
  fi
done

# Check endpoints
echo ""
for endpoint in "http://127.0.0.1:8000/api/health:Backend" "http://127.0.0.1:5173:Frontend"; do
  url="${endpoint%%:*}:${endpoint#*:}"
  label="${endpoint##*:}"
  url="${endpoint%:*}"
  if curl -sf "$url" >/dev/null 2>&1; then
    ok "$label responding at $url"
  else
    warn "$label not responding at $url"
  fi
done
echo ""
