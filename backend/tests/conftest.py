from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Tests import the app package the same way the CLI and uvicorn do.
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


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
    """Point global-config lookup at an empty temp dir so the host config cannot leak in."""
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    monkeypatch.delenv('AMC_MODEL', raising=False)
    return tmp_path / 'xdg'
