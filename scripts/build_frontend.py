#!/usr/bin/env python3
"""Build the React frontend and sync its static bundle into backend/app/static/ui.

Usage:
    python scripts/build_frontend.py              # run `npm run build` then sync
    python scripts/build_frontend.py --copy-only  # sync existing frontend/dist
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / 'frontend'
DIST_DIR = FRONTEND_DIR / 'dist'
TARGET_DIR = ROOT / 'backend' / 'app' / 'static' / 'ui'


def _resolve_npm() -> str:
    npm = shutil.which('npm')
    if npm:
        return npm
    if sys.platform == 'win32':
        npm_cmd = shutil.which('npm.cmd')
        if npm_cmd:
            return npm_cmd
    raise RuntimeError('npm not found on PATH; install Node.js 22+ to rebuild frontend assets.')


def build_and_sync(*, copy_only: bool = False) -> Path:
    if not copy_only:
        npm = _resolve_npm()
        subprocess.run([npm, 'run', 'build'], cwd=str(FRONTEND_DIR), check=True)

    index_file = DIST_DIR / 'index.html'
    if not index_file.is_file():
        raise FileNotFoundError(f'Built frontend index not found: {index_file}')

    if TARGET_DIR.exists():
        shutil.rmtree(TARGET_DIR)
    shutil.copytree(DIST_DIR, TARGET_DIR)
    return TARGET_DIR


def main() -> None:
    parser = argparse.ArgumentParser(description='Bundle built frontend assets into the Python package.')
    parser.add_argument('--copy-only', action='store_true', help='Skip `npm run build` and copy existing frontend/dist.')
    args = parser.parse_args()
    out = build_and_sync(copy_only=args.copy_only)
    print(f'Synced frontend bundle to {out}')


if __name__ == '__main__':
    main()
