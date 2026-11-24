"""P5 regressions: platform data dir, legacy DB import, bundled UI, packaged CLI, and local security."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.cli as cli
from app.api.security import LocalSecurityMiddleware, is_allowed_host, is_allowed_origin, parse_host_header
from app.core import paths
from app.core.db import Database
from app.core.models import Task
from app.core.worktree import WorktreeManager


# ── 1. Platform data directory paths ──────────────────────────────────────


def test_data_dir_platform_defaults_and_overrides(tmp_path, monkeypatch):
    monkeypatch.delenv('AMC_DATA_DIR', raising=False)
    monkeypatch.delenv('AMC_DB_PATH', raising=False)
    monkeypatch.delenv('AMC_RUNTIME_DIR', raising=False)
    monkeypatch.delenv('AMC_WORKTREES_DIR', raising=False)
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path / 'home'))

    monkeypatch.setattr(paths.sys, 'platform', 'darwin')
    assert paths.get_data_dir() == tmp_path / 'home' / 'Library' / 'Application Support' / 'amc'
    assert paths.get_db_path() == tmp_path / 'home' / 'Library' / 'Application Support' / 'amc' / 'amc.db'
    assert paths.get_worktrees_dir() == tmp_path / 'home' / 'Library' / 'Application Support' / 'amc' / 'wt'

    monkeypatch.setattr(paths.sys, 'platform', 'linux')
    monkeypatch.delenv('XDG_DATA_HOME', raising=False)
    assert paths.get_data_dir() == tmp_path / 'home' / '.local' / 'share' / 'amc'
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'xdg-data'))
    assert paths.get_data_dir() == tmp_path / 'xdg-data' / 'amc'

    monkeypatch.setattr(paths.sys, 'platform', 'win32')
    monkeypatch.setenv('APPDATA', str(tmp_path / 'roaming'))
    assert paths.get_data_dir() == tmp_path / 'roaming' / 'amc'
    monkeypatch.delenv('APPDATA', raising=False)
    assert paths.get_data_dir() == tmp_path / 'home' / 'AppData' / 'Roaming' / 'amc'

    # Explicit overrides win across all platforms
    monkeypatch.setenv('AMC_DATA_DIR', str(tmp_path / 'custom-data'))
    assert paths.get_data_dir() == (tmp_path / 'custom-data').resolve()
    monkeypatch.setenv('AMC_DB_PATH', str(tmp_path / 'override.db'))
    monkeypatch.setenv('AMC_RUNTIME_DIR', str(tmp_path / 'rt'))
    monkeypatch.setenv('AMC_WORKTREES_DIR', str(tmp_path / 'short-wt'))
    assert paths.get_db_path() == (tmp_path / 'override.db').resolve()
    assert paths.get_runtime_dir() == (tmp_path / 'rt').resolve()
    assert paths.get_worktrees_dir() == (tmp_path / 'short-wt').resolve()


# ── 2. Legacy DB atomic import ────────────────────────────────────────────


def test_legacy_db_import_copies_wal_data_without_touching_source(tmp_path, monkeypatch):
    monkeypatch.delenv('AMC_NO_LEGACY_IMPORT', raising=False)
    monkeypatch.delenv('AMC_DB_PATH', raising=False)

    legacy_dir = tmp_path / 'legacy_repo' / 'data'
    legacy_db_path = legacy_dir / 'amc.db'
    legacy_db = Database(str(legacy_db_path))
    # Enable WAL and insert a task so we verify sqlite3.backup() captures WAL state cleanly.
    legacy_db.conn.execute('PRAGMA journal_mode=WAL')
    task = Task(title='Legacy review', repo_path=str(tmp_path / 'legacy_repo'), created_at=1.0, updated_at=1.0)
    legacy_db.save_task(task)
    legacy_db.close()

    target_db_path = tmp_path / 'app-data' / 'amc.db'
    imported = paths.maybe_import_legacy_db(target_db_path, candidates=[legacy_db_path])
    assert imported == legacy_db_path.resolve()
    assert target_db_path.exists()
    # Original legacy DB must remain intact
    assert legacy_db_path.exists()

    migrated = Database(str(target_db_path))
    loaded = migrated.get_task(task.id)
    assert loaded is not None
    assert loaded.title == 'Legacy review'
    migrated.close()

    # Second run must NOT overwrite an existing target DB
    second_legacy = tmp_path / 'other' / 'data' / 'amc.db'
    db2 = Database(str(second_legacy))
    db2.save_task(Task(title='Should not overwrite', repo_path=str(tmp_path), created_at=1.0, updated_at=1.0))
    db2.close()
    assert paths.maybe_import_legacy_db(target_db_path, candidates=[second_legacy]) is None


def test_legacy_db_import_skips_empty_garbage_and_opt_out(tmp_path, monkeypatch):
    monkeypatch.delenv('AMC_NO_LEGACY_IMPORT', raising=False)
    monkeypatch.delenv('AMC_DB_PATH', raising=False)

    # Non-SQLite file is skipped
    garbage = tmp_path / 'bad.db'
    garbage.write_text('not a sqlite database', encoding='utf-8')
    # Empty schema-only AMC DB (0 tasks) is skipped
    empty_db_path = tmp_path / 'empty.db'
    empty_db = Database(str(empty_db_path))
    empty_db.close()

    target = tmp_path / 'data' / 'amc.db'
    assert paths.maybe_import_legacy_db(target, candidates=[garbage, empty_db_path]) is None
    assert not target.exists()

    # Opt-out flag skips even valid non-empty legacy DB
    valid_path = tmp_path / 'valid.db'
    vdb = Database(str(valid_path))
    vdb.save_task(Task(title='Real task', repo_path=str(tmp_path), created_at=1.0, updated_at=1.0))
    vdb.close()

    monkeypatch.setenv('AMC_NO_LEGACY_IMPORT', '1')
    assert paths.maybe_import_legacy_db(target, candidates=[valid_path]) is None
    assert not target.exists()


# ── 3. Worktree short path & bounded cleanup retry ───────────────────────


def test_worktree_custom_short_root_and_cleanup_retry(git_repo, tmp_path, monkeypatch):
    wt_root = tmp_path / 'wt'
    mgr = WorktreeManager(str(tmp_path / 'rt'), worktrees_root=wt_root)
    task = Task(title='Short wt', repo_path=str(git_repo), created_at=1.0, updated_at=1.0)
    branch, wt_path, _rev = mgr.ensure_worktree(task)
    assert Path(wt_path) == wt_root / task.id
    assert Path(wt_path).is_dir()

    # Simulate transient Windows lock on first `git worktree remove` call, succeeding on retry
    original_run_git = WorktreeManager._run_git
    remove_attempts = {'count': 0}

    def flaky_run_git(repo_path, args, *, timeout):
        if args[:2] == ['worktree', 'remove']:
            remove_attempts['count'] += 1
            if remove_attempts['count'] == 1:
                return subprocess.CompletedProcess(args, 1, stdout='', stderr='Permission denied (transient lock)')
        return original_run_git(repo_path, args, timeout=timeout)

    sleeps: list[float] = []
    monkeypatch.setattr(mgr, '_run_git', flaky_run_git)
    monkeypatch.setattr('app.core.worktree.time.sleep', lambda sec: sleeps.append(sec))

    result = mgr.cleanup_worktree(task)
    assert result['removed'] is True
    assert result['exists_after'] is False
    assert remove_attempts['count'] == 2
    assert len(sleeps) == 1


# ── 4. Local Host / Origin security middleware ───────────────────────────


def test_host_and_origin_parsers_reject_spoofing_and_allow_loopback():
    assert parse_host_header('127.0.0.1:8000') == '127.0.0.1'
    assert parse_host_header('localhost') == 'localhost'
    assert parse_host_header('[::1]:8000') == '::1'
    assert parse_host_header('127.0.0.1@evil.com') is None
    assert parse_host_header('evil.com/foo') is None
    assert parse_host_header('127.0.0.1:notaport') is None
    assert parse_host_header('') is None

    assert is_allowed_host('127.0.0.1:8000', client_host='127.0.0.1')
    assert is_allowed_host('localhost:5173', client_host='127.0.0.1')
    assert is_allowed_host('[::1]:8000', client_host='::1')
    assert is_allowed_host('testserver', client_host='testclient')
    # Real non-TestClient connection must NOT be allowed to spoof Host: testserver
    assert not is_allowed_host('testserver', client_host='127.0.0.1')
    assert not is_allowed_host('evil.com', client_host='127.0.0.1')
    assert not is_allowed_host('127.0.0.1.evil.com', client_host='127.0.0.1')
    assert not is_allowed_host('192.168.1.5:8000', client_host='192.168.1.5')

    assert is_allowed_origin('http://127.0.0.1:8000')
    assert is_allowed_origin('http://localhost:5173')
    assert is_allowed_origin('http://[::1]:5173')
    assert not is_allowed_origin('null')
    assert not is_allowed_origin('https://evil.com')
    assert not is_allowed_origin('http://localhost.evil.com')
    assert not is_allowed_origin('file:///etc/passwd')
    assert not is_allowed_origin('http://user@127.0.0.1:8000')


def test_local_security_middleware_blocks_dns_rebinding_and_csrf():
    mini = FastAPI()
    mini.add_middleware(LocalSecurityMiddleware)

    @mini.get('/api/health')
    def _health():
        return {'ok': True}

    @mini.post('/api/admin/shutdown')
    def _shutdown():
        return {'ok': True}

    client = TestClient(mini)

    # Valid loopback Host & Origin
    assert client.get('/api/health', headers={'host': '127.0.0.1:8000'}).status_code == 200
    assert client.get('/api/health', headers={'host': 'localhost:8000'}).status_code == 200
    assert client.get('/api/health', headers={'host': '[::1]:8000'}).status_code == 200
    assert client.post(
        '/api/admin/shutdown',
        headers={'host': '127.0.0.1:8000', 'origin': 'http://localhost:5173'},
    ).status_code == 200
    # CLI call without Origin/Referer/Sec-Fetch-Site is allowed
    assert client.post('/api/admin/shutdown', headers={'host': '127.0.0.1:8000'}).status_code == 200

    # DNS rebinding blocked on GET & POST
    r_bad_host = client.get('/api/health', headers={'host': 'rebind.evil.com:8000'})
    assert r_bad_host.status_code == 403
    assert 'Host' in r_bad_host.json()['detail']

    # Cross-origin Origin blocked on GET & POST (including Origin: null)
    assert client.get('/api/health', headers={'host': '127.0.0.1:8000', 'origin': 'https://evil.com'}).status_code == 403
    assert client.post('/api/admin/shutdown', headers={'host': '127.0.0.1:8000', 'origin': 'null'}).status_code == 403
    assert client.post(
        '/api/admin/shutdown',
        headers={'host': '127.0.0.1:8000', 'origin': 'https://evil.com'},
    ).status_code == 403

    # Sec-Fetch-Site: cross-site blocked on POST even without Origin
    assert client.post(
        '/api/admin/shutdown',
        headers={'host': '127.0.0.1:8000', 'sec-fetch-site': 'cross-site'},
    ).status_code == 403

    # Cross-origin Referer blocked on POST when Origin is omitted
    assert client.post(
        '/api/admin/shutdown',
        headers={'host': '127.0.0.1:8000', 'referer': 'https://evil.com/csrf.html'},
    ).status_code == 403
    assert client.post(
        '/api/admin/shutdown',
        headers={'host': '127.0.0.1:8000', 'referer': 'http://127.0.0.1:8000/'},
    ).status_code == 200


# ── 5. Bundled UI serving in FastAPI app ─────────────────────────────────


def test_bundled_ui_served_by_fastapi_and_blocks_traversal():
    from app.api.app import app

    assert paths.has_bundled_ui(), 'backend/app/static/ui/index.html must be built and present'
    client = TestClient(app)

    res_health = client.get('/api/health', headers={'host': '127.0.0.1:8000'})
    assert res_health.status_code == 200
    assert res_health.json() == {'ok': True}

    res_root = client.get('/', headers={'host': '127.0.0.1:8000'})
    assert res_root.status_code == 200
    html = res_root.text
    assert '<div id="root">' in html
    assert '/assets/' in html

    # Verify bundled JS/CSS assets load with 200
    assets_dir = paths.get_bundled_ui_dir() / 'assets'
    js_files = list(assets_dir.glob('*.js'))
    css_files = list(assets_dir.glob('*.css'))
    assert js_files and css_files
    assert client.get(f'/assets/{js_files[0].name}', headers={'host': '127.0.0.1:8000'}).status_code == 200
    assert client.get(f'/assets/{css_files[0].name}', headers={'host': '127.0.0.1:8000'}).status_code == 200

    # Unknown /api/* still returns 404 (not index.html)
    assert client.get('/api/does-not-exist', headers={'host': '127.0.0.1:8000'}).status_code == 404
    # Security middleware is active on the real app
    assert client.get('/api/health', headers={'host': 'evil.com'}).status_code == 403


# ── 6. Packaged CLI mode (no Node, no .venv, no pyproject.toml) ──────────


def test_cli_packaged_mode_starts_without_node_or_venv(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / 'pkg-data'
    monkeypatch.setenv('AMC_DATA_DIR', str(data_dir))
    monkeypatch.delenv('AMC_ROOT', raising=False)
    monkeypatch.setattr(cli, '_USE_COLOR', False)
    monkeypatch.setattr(cli, 'IS_WINDOWS', False)
    # Simulate running from site-packages (no pyproject.toml anywhere above cli.py)
    monkeypatch.setattr(cli, '_root_dir', lambda: data_dir)
    monkeypatch.setattr(cli, '_ensure_venv', lambda: pytest.fail('packaged mode must not bootstrap .venv'))
    monkeypatch.setattr(cli, '_ensure_frontend_deps', lambda: pytest.fail('packaged mode must not run npm'))
    monkeypatch.setattr(cli, '_which', lambda _name: None)
    monkeypatch.setattr(cli, '_resolve_npm', lambda: None)
    monkeypatch.setattr(cli, '_detect_backends', lambda: ['codex'])
    monkeypatch.setattr(cli, '_find_free_port', lambda port: port)
    monkeypatch.setattr(cli, '_health_ok', lambda _port: True)
    monkeypatch.setattr(cli.signal, 'signal', lambda *_args: None)

    port = 18260
    pid = 51515
    identity = {
        'pid': pid,
        'start_time': 'Tue Nov 24 14:00:00 2026',
        'command': f'{sys.executable} -m uvicorn app.api.app:app --host 127.0.0.1 --port {port}',
        'session_id': pid,
        'pgid': pid,
        'source': 'ps',
    }
    monkeypatch.setattr(cli, '_process_identity', lambda candidate: identity.copy() if candidate == pid else None)

    class FakeProc:
        def __init__(self):
            self.pid = pid
            self.returncode = 0

        def poll(self):
            return 0

    spawned: list[list[str]] = []
    monkeypatch.setattr(cli.subprocess, 'Popen', lambda cmd, **_kwargs: spawned.append(cmd) or FakeProc())

    args = SimpleNamespace(no_ui=False, bundled_ui=False, no_browser=True, port=port, ui_port=18261)
    cli.cmd_start(args)

    captured = capsys.readouterr()
    assert 'Node.js not found' not in captured.err
    assert f'http://127.0.0.1:{port}' in captured.out
    assert len(spawned) == 1
    assert spawned[0][:4] == [sys.executable, '-m', 'uvicorn', 'app.api.app:app']
    assert '--app-dir' not in spawned[0]
    assert (data_dir / 'run' / 'ports.json').is_file()
    state = json.loads((data_dir / 'run' / 'backend.json').read_text(encoding='utf-8'))
    assert cli._verify_service_state(state)[0] is True
