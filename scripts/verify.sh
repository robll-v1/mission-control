#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

cd "$ROOT_DIR"
. .venv/bin/activate

python -m compileall backend/app

cd "$ROOT_DIR/frontend"
npm run build
