from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_SCRIPT = REPO_ROOT / 'examples' / 'first-review' / 'make_demo_repo.py'


def _run_fixture(target: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(FIXTURE_SCRIPT), str(target), *args],
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        check=False,
    )


@pytest.fixture
def demo_repo(tmp_path: Path) -> Path:
    target = tmp_path / 'synthetic-demo'
    result = _run_fixture(target)
    assert result.returncode == 0, result.stderr
    return target


def test_generated_fixture_notes_use_lf_bytes(demo_repo: Path):
    notes = (demo_repo / 'notes.py').read_bytes()

    assert b'\r\n' not in notes
    assert b'\n' in notes


def test_apply_fix_changes_generated_fixture_and_is_idempotent(demo_repo: Path):
    first = _run_fixture(demo_repo, '--apply-fix')

    assert first.returncode == 0, first.stderr
    fixed_contents = (demo_repo / 'notes.py').read_bytes()
    assert 'ESCAPE' in fixed_contents.decode('utf-8')

    second = _run_fixture(demo_repo, '--apply-fix')

    assert second.returncode == 0, second.stderr
    assert 'already applied' in second.stdout
    assert (demo_repo / 'notes.py').read_bytes() == fixed_contents


def test_apply_fix_refuses_unrecognized_notes_without_changing_bytes(demo_repo: Path):
    notes = demo_repo / 'notes.py'
    unexpected = b'user-authored notes\nwith bytes preserved: \xff\n'
    notes.write_bytes(unexpected)

    result = _run_fixture(demo_repo, '--apply-fix')

    assert result.returncode != 0
    assert 'refusing to overwrite' in result.stderr
    assert notes.read_bytes() == unexpected


def test_apply_fix_refuses_non_git_target_without_changing_bytes(tmp_path: Path):
    target = tmp_path / 'not-a-repo'
    target.mkdir()
    notes = target / 'notes.py'
    original = b'keep this file\n'
    notes.write_bytes(original)

    result = _run_fixture(target, '--apply-fix')

    assert result.returncode != 0
    assert 'not a generated first-review fixture repo' in result.stderr
    assert notes.read_bytes() == original
