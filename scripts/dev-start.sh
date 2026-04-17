#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

BACKEND_PORT="${AMC_PORT:-8000}"
FRONTEND_PORT="${AMC_UI_PORT:-5173}"
RUN_DIR="$ROOT_DIR/.run"

# --- helpers ---
info()  { printf '\033[0;36m%s\033[0m\n' "$*"; }
ok()    { printf '\033[0;32m✅ %s\033[0m\n' "$*"; }
warn()  { printf '\033[0;33m⚠️  %s\033[0m\n' "$*"; }
fail()  { printf '\033[0;31m❌ %s\033[0m\n' "$*"; exit 1; }

# --- 1. check prerequisites ---
info "Checking prerequisites..."

# Python 3.11+
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" &>/dev/null; then
    ver=$("$candidate" -c 'import sys; print(sys.version_info[:2])' 2>/dev/null || echo "(0,0)")
    major=$(echo "$ver" | sed 's/[^0-9,]//g' | cut -d, -f1)
    minor=$(echo "$ver" | sed 's/[^0-9,]//g' | cut -d, -f2)
    if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
      PYTHON="$candidate"
      break
    fi
  fi
done
[ -z "$PYTHON" ] && fail "Python 3.11+ is required but not found."
ok "Python: $($PYTHON --version)"

# Node.js (for frontend)
if ! command -v node &>/dev/null; then
  warn "Node.js not found. Frontend will not be started."
  SKIP_FRONTEND=1
else
  ok "Node.js: $(node --version)"
  SKIP_FRONTEND=0
fi

# OpenCode
if ! command -v opencode &>/dev/null; then
  warn "OpenCode not found in PATH. Reviews will fail until it is installed."
else
  ok "OpenCode: found"
fi

# GitHub token
if [ -z "${GITHUB_TOKEN:-}" ] && [ -z "${GH_TOKEN:-}" ]; then
  warn "No GITHUB_TOKEN or GH_TOKEN set. GitHub API rate limits will apply."
fi

# --- 2. auto-bootstrap if needed ---
if [ ! -d "$ROOT_DIR/.venv" ]; then
  info "First run — setting up virtual environment..."
  "$PYTHON" -m venv "$ROOT_DIR/.venv"
  . "$ROOT_DIR/.venv/bin/activate"
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet -e "$ROOT_DIR"
  ok "Python environment ready"
else
  . "$ROOT_DIR/.venv/bin/activate"
  ok "Using existing virtual environment"
fi

if [ "$SKIP_FRONTEND" = "0" ] && [ ! -d "$ROOT_DIR/frontend/node_modules" ]; then
  info "Installing frontend dependencies..."
  cd "$ROOT_DIR/frontend" && npm install --silent
  cd "$ROOT_DIR"
  ok "Frontend dependencies ready"
fi

# --- 3. find available ports ---
find_free_port() {
  local preferred=$1
  local port=$preferred
  while lsof -i:"$port" -t >/dev/null 2>&1; do
    port=$((port + 1))
    if [ "$port" -gt $((preferred + 20)) ]; then
      fail "Could not find a free port near $preferred"
    fi
  done
  echo "$port"
}

BACKEND_PORT=$(find_free_port "$BACKEND_PORT")
FRONTEND_PORT=$(find_free_port "$FRONTEND_PORT")
if [ "$BACKEND_PORT" != "${AMC_PORT:-8000}" ]; then
  info "Port ${AMC_PORT:-8000} in use, using $BACKEND_PORT for backend"
fi
if [ "$FRONTEND_PORT" != "${AMC_UI_PORT:-5173}" ]; then
  info "Port ${AMC_UI_PORT:-5173} in use, using $FRONTEND_PORT for frontend"
fi

# --- 4. start services ---
mkdir -p "$RUN_DIR"

info "Starting backend on port $BACKEND_PORT..."
uvicorn app.api.app:app --app-dir "$ROOT_DIR/backend" \
  --host 127.0.0.1 --port "$BACKEND_PORT" --reload \
  > "$RUN_DIR/backend.log" 2>&1 &
BACKEND_PID=$!
echo "$BACKEND_PID" > "$RUN_DIR/backend.pid"

if [ "$SKIP_FRONTEND" = "0" ]; then
  info "Starting frontend on port $FRONTEND_PORT..."
  cd "$ROOT_DIR/frontend"
  VITE_API_TARGET="http://127.0.0.1:$BACKEND_PORT" \
    npm run dev -- --host 127.0.0.1 --port "$FRONTEND_PORT" \
    > "$RUN_DIR/frontend.log" 2>&1 &
  FRONTEND_PID=$!
  echo "$FRONTEND_PID" > "$RUN_DIR/frontend.pid"
  cd "$ROOT_DIR"
fi

# --- 5. wait for backend health ---
info "Waiting for backend to be ready..."
READY=0
for i in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:$BACKEND_PORT/api/health" >/dev/null 2>&1; then
    READY=1
    break
  fi
  # check process is still alive
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    echo ""
    fail "Backend process exited unexpectedly. Check $RUN_DIR/backend.log"
  fi
  sleep 1
done

if [ "$READY" = "0" ]; then
  fail "Backend did not become healthy within 30s. Check $RUN_DIR/backend.log"
fi

# --- 6. done ---
echo ""
ok "Mission Control is running!"
echo ""
echo "   Web UI:   http://localhost:$FRONTEND_PORT"
echo "   API:      http://localhost:$BACKEND_PORT"
echo "   Health:   http://localhost:$BACKEND_PORT/api/health"
echo ""
echo "   Logs:     $RUN_DIR/backend.log"
[ "$SKIP_FRONTEND" = "0" ] && echo "              $RUN_DIR/frontend.log"
echo "   Stop:     make stop"
echo ""

# Open browser on macOS
if command -v open &>/dev/null && [ "$SKIP_FRONTEND" = "0" ]; then
  open "http://localhost:$FRONTEND_PORT" 2>/dev/null || true
fi

# Stay in foreground, relay Ctrl+C to child processes
cleanup() {
  echo ""
  info "Shutting down..."
  kill "$BACKEND_PID" 2>/dev/null || true
  [ "${FRONTEND_PID:-}" ] && kill "$FRONTEND_PID" 2>/dev/null || true
  rm -rf "$RUN_DIR"
  ok "All services stopped."
  exit 0
}
trap cleanup INT TERM

wait
