from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# Tests import the app package the same way the CLI and uvicorn do.
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Isolate the process-wide default AMC_DATA_DIR before any test imports
# `app.api.app`, so pytest never reads, mutates, or migrates the developer's
# real application data directory. Also allow FastAPI TestClient's default
# `testserver` Host strictly inside pytest (never in production code).
os.environ.setdefault('AMC_ALLOWED_HOSTS', 'testserver')
if not os.environ.get('AMC_DATA_DIR'):
    _PYTEST_DATA_DIR = tempfile.mkdtemp(prefix='amc-pytest-data-')
    os.environ['AMC_DATA_DIR'] = _PYTEST_DATA_DIR
    os.environ.setdefault('AMC_NO_LEGACY_IMPORT', '1')
    atexit.register(shutil.rmtree, _PYTEST_DATA_DIR, True)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ['git', '-C', str(repo), *args],
        capture_output=True, text=True, encoding='utf-8', errors='replace', check=False,
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A tiny git repo with two commits; the second subject contains U+2014."""
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(repo)], capture_output=True, check=True)
    _git(repo, 'config', 'user.email', 'test@example.com')
    _git(repo, 'config', 'user.name', 'Test')
    _git(repo, 'config', 'commit.gpgsign', 'false')

    (repo / 'a.txt').write_text('one\n', encoding='utf-8')
    _git(repo, 'add', '-A')
    subprocess.run(['git', '-C', str(repo), 'commit', '-q', '-m', 'base commit'],
                   capture_output=True, check=True)

    (repo / 'a.txt').write_text('two\n', encoding='utf-8')
    _git(repo, 'add', '-A')
    # Mirrors this project's own history, which is what first exposed the bug.
    subprocess.run(
        ['git', '-C', str(repo), 'commit', '-q', '-m',
         'fix: Tier-2 Windows compatibility — diff paths, adapters, validation'],
        capture_output=True, check=True,
    )
    return repo


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point global-config and app-data lookups at empty temp dirs so host state cannot leak in."""
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    monkeypatch.setenv('AMC_DATA_DIR', str(tmp_path / 'amc-data'))
    monkeypatch.delenv('AMC_DB_PATH', raising=False)
    monkeypatch.delenv('AMC_RUNTIME_DIR', raising=False)
    monkeypatch.delenv('AMC_WORKTREES_DIR', raising=False)
    monkeypatch.delenv('AMC_BACKEND', raising=False)
    monkeypatch.delenv('AMC_MODEL', raising=False)
    monkeypatch.delenv('AMC_REASONING', raising=False)
    return tmp_path / 'xdg'
