#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

cd "$ROOT_DIR"
. .venv/bin/activate

exec uvicorn app.api.app:app --app-dir backend --host 127.0.0.1 --port 8000 --reload
