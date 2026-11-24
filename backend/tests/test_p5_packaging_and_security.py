"""P5 regressions: platform data dir, legacy DB import, bundled UI, packaged CLI, and local security."""
from __future__ import annotations

import json
import os
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

    monkeypatch.setenv('AMC_DATA_DIR', str(tmp_path / 'custom-data'))
    assert paths.get_data_dir() == (tmp_path / 'custom-data').resolve()
    monkeypatch.setenv('AMC_DB_PATH', str(tmp_path / 'override.db'))
    monkeypatch.setenv('AMC_RUNTIME_DIR', str(tmp_path / 'rt'))
    monkeypatch.setenv('AMC_WORKTREES_DIR', str(tmp_path / 'short-wt'))
    assert paths.get_db_path() == (tmp_path / 'override.db').resolve()
    assert paths.get_runtime_dir() == (tmp_path / 'rt').resolve()
    assert paths.get_worktrees_dir() == (tmp_path / 'short-wt').resolve()


# ── 2. Legacy DB atomic import (live WAL, schema check, symlink, no overwrite) ─


def test_legacy_db_import_reads_live_wal_readonly_without_overwriting_target(tmp_path, monkeypatch):
    monkeypatch.delenv('AMC_NO_LEGACY_IMPORT', raising=False)
    monkeypatch.delenv('AMC_DB_PATH', raising=False)

    legacy_dir = tmp_path / 'legacy_repo' / 'data'
    legacy_db_path = legacy_dir / 'amc.db'
    legacy_db = Database(str(legacy_db_path))
    legacy_db.conn.execute('PRAGMA journal_mode=WAL')
    # Keep legacy_db OPEN while importing so WAL frames remain live on disk.
    task = Task(title='Live WAL legacy review', repo_path=str(tmp_path / 'legacy_repo'), created_at=1.0, updated_at=1.0)
    legacy_db.save_task(task)
    wal_path = Path(f'{legacy_db_path}-wal')
    assert wal_path.exists() and wal_path.stat().st_size > 0
    before_bytes = legacy_db_path.read_bytes()

    target_db_path = tmp_path / 'app-data' / 'amc.db'
    try:
        imported = paths.maybe_import_legacy_db(target_db_path, candidates=[legacy_db_path])
        assert imported == legacy_db_path.resolve()
        # Read-only import must not rewrite or checkpoint the source DB file
        assert legacy_db_path.read_bytes() == before_bytes
    finally:
        legacy_db.close()

    migrated = Database(str(target_db_path))
    loaded = migrated.get_task(task.id)
    assert loaded is not None
    assert loaded.title == 'Live WAL legacy review'
    migrated.close()

    # Atomic publish must NEVER overwrite an existing target DB
    temp_copy = tmp_path / 'app-data' / 'other.tmp'
    temp_copy.write_bytes(b'new-content')
    assert paths._publish_no_overwrite(temp_copy, target_db_path) is False
    migrated2 = Database(str(target_db_path))
    assert migrated2.get_task(task.id) is not None
    migrated2.close()


def test_legacy_db_import_rejects_unrelated_db_symlinks_and_non_amc_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv('AMC_NO_LEGACY_IMPORT', raising=False)
    monkeypatch.delenv('AMC_DB_PATH', raising=False)

    # 1. Unrelated SQLite DB with a non-AMC `tasks` table (no `data` JSON, no `runs`/`events`)
    unrelated = tmp_path / 'unrelated.db'
    conn = sqlite3.connect(str(unrelated))
    conn.execute('CREATE TABLE tasks (id INTEGER PRIMARY KEY, name TEXT)')
    conn.execute("INSERT INTO tasks (name) VALUES ('buy milk')")
    conn.commit()
    conn.close()

    # 2. Empty AMC DB (0 tasks)
    empty_db_path = tmp_path / 'empty.db'
    empty_db = Database(str(empty_db_path))
    empty_db.close()

    # 3. Symlink pointing to a valid AMC DB (must not be followed)
    real_amc_path = tmp_path / 'real.db'
    real_db = Database(str(real_amc_path))
    real_db.save_task(Task(title='Real task', repo_path=str(tmp_path), created_at=1.0, updated_at=1.0))
    real_db.close()
    symlink_path = tmp_path / 'symlink.db'
    try:
        symlink_path.symlink_to(real_amc_path)
        candidates = [unrelated, empty_db_path, symlink_path]
    except OSError:
        candidates = [unrelated, empty_db_path]

    target = tmp_path / 'app-data' / 'amc.db'
    assert paths.maybe_import_legacy_db(target, candidates=candidates) is None
    assert not target.exists()

    # 4. find_legacy_db_paths NEVER trusts caller CWD (even if CWD plants a fake pyproject.toml)
    random_repo = tmp_path / 'other-project'
    (random_repo / 'data').mkdir(parents=True)
    (random_repo / 'pyproject.toml').write_text('[project]\nname = "agent-mission-control"\n', encoding='utf-8')
    (random_repo / 'data' / 'amc.db').write_bytes(real_amc_path.read_bytes())
    monkeypatch.chdir(random_repo)
    assert paths.find_legacy_db_paths() == []

    # 5. Corrupt second row in tasks table causes validation to reject the legacy DB
    corrupt_second = tmp_path / 'corrupt-second.db'
    cdb = Database(str(corrupt_second))
    cdb.save_task(Task(title='Good first row', repo_path=str(tmp_path), created_at=1.0, updated_at=1.0))
    cdb.conn.execute("INSERT INTO tasks (id, data) VALUES ('bad-row', '{not-valid-task-json')")
    cdb.conn.commit()
    cdb.close()
    assert paths.maybe_import_legacy_db(target, candidates=[corrupt_second]) is None
    assert not target.exists()

    # 6. Symlinked parent `data/` directory under an explicit checkout root is rejected
    sym_checkout = tmp_path / 'sym-checkout'
    sym_checkout.mkdir()
    (sym_checkout / 'pyproject.toml').write_text('[project]\nname = "agent-mission-control"\n', encoding='utf-8')
    outside_data = tmp_path / 'outside-data'
    outside_data.mkdir()
    (outside_data / 'amc.db').write_bytes(real_amc_path.read_bytes())
    try:
        (sym_checkout / 'data').symlink_to(outside_data, target_is_directory=True)
        assert paths.find_legacy_db_paths(extra_roots=[sym_checkout]) == []
    except OSError:
        pass

    # 7. Default maybe_import_legacy_db() (as called by ReviewEngine / SDK) imports from
    #    get_trusted_checkout_roots() without needing callers to pass extra_roots manually.
    trusted_checkout = tmp_path / 'trusted-amc-checkout'
    (trusted_checkout / 'data').mkdir(parents=True)
    (trusted_checkout / 'pyproject.toml').write_text('[project]\nname = "agent-mission-control"\n', encoding='utf-8')
    (trusted_checkout / 'data' / 'amc.db').write_bytes(real_amc_path.read_bytes())
    monkeypatch.setattr(paths, 'get_trusted_checkout_roots', lambda: [trusted_checkout])
    sdk_target = tmp_path / 'sdk-app-data' / 'amc.db'
    assert paths.maybe_import_legacy_db(sdk_target) == (trusted_checkout / 'data' / 'amc.db').resolve()
    assert sdk_target.is_file()

    # 8. Opt-out flag skips even valid direct candidate
    monkeypatch.setenv('AMC_NO_LEGACY_IMPORT', '1')
    assert paths.maybe_import_legacy_db(target, candidates=[real_amc_path]) is None
    assert not target.exists()


# ── 3. DataDirLock mutual exclusion ──────────────────────────────────────


def test_data_dir_lock_prevents_concurrent_backends(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / 'shared-data'
    data_dir.mkdir()
    # Hold the OS lock in a separate child process so POSIX flock / Windows msvcrt
    # semantics are tested across real processes.
    helper = (
        'import sys, time; from pathlib import Path; '
        'from app.core.paths import DataDirLock; '
        'lk = DataDirLock(Path(sys.argv[1])); '
        'assert lk.acquire(); print("LOCKED", flush=True); '
        'sys.stdin.read(); lk.release()'
    )
    env = os.environ.copy()
    env['PYTHONPATH'] = str(Path(__file__).resolve().parent.parent)
    proc = subprocess.Popen(
        [sys.executable, '-c', helper, str(data_dir)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        line = proc.stdout.readline().strip()
        assert line == 'LOCKED', f'child failed: {proc.stderr.read()}'
        locked, holder_pid = paths.probe_data_dir_lock(data_dir)
        assert locked is True
        assert holder_pid == proc.pid

        # `amc start` refuses cleanly when another backend holds the data dir lock
        monkeypatch.setenv('AMC_DATA_DIR', str(data_dir))
        monkeypatch.delenv('AMC_ROOT', raising=False)
        monkeypatch.setattr(cli, '_USE_COLOR', False)
        monkeypatch.setattr(cli, '_root_dir', lambda: data_dir)
        with pytest.raises(SystemExit) as exc_info:
            cli.cmd_start(SimpleNamespace(no_ui=True, bundled_ui=False, no_browser=True, port=18270, ui_port=18271))
        assert exc_info.value.code == 1
        assert 'already using data directory' in capsys.readouterr().err
    finally:
        if proc.stdin:
            proc.stdin.close()
        proc.wait(timeout=10)

    locked_after, _ = paths.probe_data_dir_lock(data_dir)
    assert locked_after is False


# ── 4. Worktree short path & bounded cleanup retry ───────────────────────


def test_worktree_custom_short_root_and_cleanup_retry(git_repo, tmp_path, monkeypatch):
    wt_root = tmp_path / 'wt'
    mgr = WorktreeManager(str(tmp_path / 'rt'), worktrees_root=wt_root)
    task = Task(title='Short wt', repo_path=str(git_repo), created_at=1.0, updated_at=1.0)
    branch, wt_path, _rev = mgr.ensure_worktree(task)
    assert Path(wt_path) == wt_root / task.id
    assert Path(wt_path).is_dir()

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


# ── 5. Local Host / Origin security middleware ───────────────────────────


def test_host_and_origin_parsers_have_no_prod_testserver_backdoor(monkeypatch):
    monkeypatch.delenv('AMC_ALLOWED_HOSTS', raising=False)
    monkeypatch.delenv('AMC_ALLOWED_ORIGINS', raising=False)

    assert parse_host_header('127.0.0.1:8000') == '127.0.0.1'
    assert parse_host_header('localhost') == 'localhost'
    assert parse_host_header('[::1]:8000') == '::1'
    assert parse_host_header('127.0.0.1@evil.com') is None
    assert parse_host_header('evil.com/foo') is None
    assert parse_host_header('127.0.0.1:notaport') is None
    assert parse_host_header('') is None

    assert is_allowed_host('127.0.0.1:8000')
    assert is_allowed_host('localhost:5173')
    assert is_allowed_host('[::1]:8000')
    # Zero hardcoded `testserver` backdoor when AMC_ALLOWED_HOSTS is unset!
    assert not is_allowed_host('testserver')
    assert not is_allowed_host('evil.com')
    assert not is_allowed_host('127.0.0.1.evil.com')
    assert not is_allowed_host('192.168.1.5:8000')

    # Exact-origin match against server Host prevents cross-port localhost attacks
    assert is_allowed_origin('http://127.0.0.1:8000', expected_host=('127.0.0.1', 8000))
    assert not is_allowed_origin('http://127.0.0.1:9999', expected_host=('127.0.0.1', 8000))
    assert not is_allowed_origin('http://localhost:8000', expected_host=('127.0.0.1', 8000))
    assert not is_allowed_origin('null', expected_host=('127.0.0.1', 8000))
    assert not is_allowed_origin('https://evil.com', expected_host=('127.0.0.1', 8000))
    assert not is_allowed_origin('file:///etc/passwd', expected_host=('127.0.0.1', 8000))
    assert not is_allowed_origin('http://user@127.0.0.1:8000', expected_host=('127.0.0.1', 8000))

    # Explicit Vite dev origin in AMC_ALLOWED_ORIGINS is honored
    monkeypatch.setenv('AMC_ALLOWED_ORIGINS', 'http://localhost:5173')
    assert is_allowed_origin('http://localhost:5173', expected_host=('127.0.0.1', 8000))
    assert not is_allowed_origin('http://localhost:9999', expected_host=('127.0.0.1', 8000))


def test_local_security_middleware_blocks_dns_rebinding_and_cross_port_csrf(monkeypatch):
    monkeypatch.delenv('AMC_ALLOWED_HOSTS', raising=False)
    monkeypatch.delenv('AMC_ALLOWED_ORIGINS', raising=False)

    mini = FastAPI()
    mini.add_middleware(LocalSecurityMiddleware)

    @mini.get('/api/health')
    def _health():
        return {'ok': True}

    @mini.post('/api/admin/shutdown')
    def _shutdown():
        return {'ok': True}

    client = TestClient(mini)

    # Host: testserver is REJECTED in production (when AMC_ALLOWED_HOSTS is unset),
    # even if X-Forwarded-For tries to spoof client host.
    assert client.get(
        '/api/health',
        headers={'host': 'testserver', 'x-forwarded-for': 'testclient'},
    ).status_code == 403

    # Valid loopback Host & same-origin POST
    assert client.get('/api/health', headers={'host': '127.0.0.1:8000'}).status_code == 200
    assert client.get('/api/health', headers={'host': 'localhost:8000'}).status_code == 200
    assert client.get('/api/health', headers={'host': '[::1]:8000'}).status_code == 200
    assert client.post(
        '/api/admin/shutdown',
        headers={'host': '127.0.0.1:8000', 'origin': 'http://127.0.0.1:8000', 'sec-fetch-site': 'same-origin'},
    ).status_code == 200
    # CLI call without Origin/Referer/Sec-Fetch-Site is allowed
    assert client.post('/api/admin/shutdown', headers={'host': '127.0.0.1:8000'}).status_code == 200

    # Cross-port localhost POST (e.g. rogue app on port 9999) is BLOCKED
    assert client.post(
        '/api/admin/shutdown',
        headers={'host': '127.0.0.1:8000', 'origin': 'http://127.0.0.1:9999'},
    ).status_code == 403
    assert client.post(
        '/api/admin/shutdown',
        headers={'host': '127.0.0.1:8000', 'sec-fetch-site': 'same-site'},
    ).status_code == 403
    assert client.post(
        '/api/admin/shutdown',
        headers={'host': '127.0.0.1:8000', 'referer': 'http://127.0.0.1:9999/attack'},
    ).status_code == 403

    # Explicitly allowed Vite origin passes
    monkeypatch.setenv('AMC_ALLOWED_ORIGINS', 'http://localhost:5173')
    assert client.post(
        '/api/admin/shutdown',
        headers={'host': '127.0.0.1:8000', 'origin': 'http://localhost:5173', 'sec-fetch-site': 'same-site'},
    ).status_code == 200

    # DNS rebinding & cross-site blocked
    assert client.get('/api/health', headers={'host': 'rebind.evil.com:8000'}).status_code == 403
    assert client.get('/api/health', headers={'host': '127.0.0.1:8000', 'origin': 'https://evil.com'}).status_code == 403
    assert client.post('/api/admin/shutdown', headers={'host': '127.0.0.1:8000', 'origin': 'null'}).status_code == 403
    assert client.post(
        '/api/admin/shutdown',
        headers={'host': '127.0.0.1:8000', 'sec-fetch-site': 'cross-site'},
    ).status_code == 403
    assert client.post(
        '/api/admin/shutdown',
        headers={'host': '127.0.0.1:8000', 'referer': 'https://evil.com/csrf.html'},
    ).status_code == 403
    assert client.post(
        '/api/admin/shutdown',
        headers={'host': '127.0.0.1:8000', 'referer': 'http://127.0.0.1:8000/'},
    ).status_code == 200


# ── 6. Bundled UI serving in FastAPI app ─────────────────────────────────


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

    assets_dir = paths.get_bundled_ui_dir() / 'assets'
    js_files = list(assets_dir.glob('*.js'))
    css_files = list(assets_dir.glob('*.css'))
    assert js_files and css_files
    assert client.get(f'/assets/{js_files[0].name}', headers={'host': '127.0.0.1:8000'}).status_code == 200
    assert client.get(f'/assets/{css_files[0].name}', headers={'host': '127.0.0.1:8000'}).status_code == 200

    assert client.get('/api/does-not-exist', headers={'host': '127.0.0.1:8000'}).status_code == 404
    assert client.get('/api/health', headers={'host': 'evil.com'}).status_code == 403


# ── 7. Packaged CLI mode (no Node, no .venv, safe sys.path -P, trusted cwd) ─


def test_cli_packaged_mode_starts_safely_without_node_or_venv(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / 'pkg-data'
    monkeypatch.setenv('AMC_DATA_DIR', str(data_dir))
    monkeypatch.delenv('AMC_ROOT', raising=False)
    monkeypatch.setattr(cli, '_USE_COLOR', False)
    monkeypatch.setattr(cli, 'IS_WINDOWS', False)
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
        'command': f'{sys.executable} -P -m uvicorn app.api.app:app --host 127.0.0.1 --port {port} --no-proxy-headers',
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

    spawned: list[tuple[list[str], str | None, dict | None]] = []
    monkeypatch.setattr(
        cli.subprocess,
        'Popen',
        lambda cmd, **kwargs: spawned.append((cmd, kwargs.get('cwd'), kwargs.get('env'))) or FakeProc(),
    )

    # Use a RELATIVE AMC_DATA_DIR to verify cli.py normalizes it to an absolute
    # path before switching cwd=data_dir for the backend subprocess.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('AMC_DATA_DIR', 'pkg-data')

    args = SimpleNamespace(no_ui=False, bundled_ui=False, no_browser=True, port=port, ui_port=18261)
    cli.cmd_start(args)

    captured = capsys.readouterr()
    assert 'Node.js not found' not in captured.err
    assert f'http://127.0.0.1:{port}' in captured.out
    assert len(spawned) == 1
    cmd, cwd, proc_env = spawned[0]
    assert cmd == [
        sys.executable, '-P', '-m', 'uvicorn', 'app.api.app:app',
        '--host', '127.0.0.1', '--port', str(port), '--no-proxy-headers',
    ]
    assert cwd == str(data_dir.resolve())
    assert proc_env is not None
    assert proc_env['AMC_DATA_DIR'] == str(data_dir.resolve())
    assert (data_dir / 'run' / 'ports.json').is_file()
    state = json.loads((data_dir / 'run' / 'backend.json').read_text(encoding='utf-8'))
    assert cli._verify_service_state(state)[0] is True
