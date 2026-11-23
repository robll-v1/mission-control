"""Offline regressions for first-run bootstrap and start/stop lifecycle.

These tests use a temporary source root (via AMC_ROOT), fake child processes
and isolated config. They never start the real backend, call an agent or touch
the user's services.
"""
from __future__ import annotations

import os
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.cli as cli


@pytest.fixture(autouse=True)
def _bootstrap_environment(isolated_config, monkeypatch):
    monkeypatch.setattr(cli, '_USE_COLOR', False)
    monkeypatch.setattr(cli, 'IS_WINDOWS', False)
    monkeypatch.setenv('NO_COLOR', '1')
    monkeypatch.delenv('AMC_ROOT', raising=False)
    cwd = os.getcwd()
    yield
    os.chdir(cwd)


def _source_root(tmp_path: Path) -> Path:
    src = tmp_path / 'src'
    src.mkdir()
    (src / 'pyproject.toml').write_text('[project]\nname = "amc-test"\n', encoding='utf-8')
    return src


def _start_args(**overrides):
    values = {
        'no_ui': True,
        'no_browser': True,
        'port': 18160,
        'ui_port': 18161,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _register_fake_identity(monkeypatch, root: Path, pid: int, *, start_time='Sun Oct 26 09:11:00 2025'):
    identity = {
        'pid': pid,
        'start_time': start_time,
        'command': f'uvicorn app.api.app:app --app-dir {root / "backend"} --port 18160',
        'session_id': pid,
        'pgid': pid,
        'source': 'ps',
    }
    monkeypatch.setattr(cli, '_process_identity', lambda candidate: identity.copy() if candidate == pid else None)
    return identity


def _write_verified_backend(run_dir: Path, root: Path, pid=12345, port=18160):
    state = {
        'schema': cli.STATE_SCHEMA,
        'name': 'backend',
        'pid': pid,
        'root': str(root.resolve()),
        'port': port,
        'signature': ['app.api.app:app', '--app-dir', str(root / 'backend'), '--port', str(port)],
        'start_time': 'Sun Oct 26 09:11:00 2025',
        'command': f'uvicorn app.api.app:app --app-dir {root / "backend"} --port {port}',
        'session_id': pid,
        'pgid': pid,
        'parent_pid': None,
    }
    cli._save_service_state('backend', state)
    return state


def test_root_dir_honours_amc_root(tmp_path, monkeypatch):
    src = _source_root(tmp_path)
    monkeypatch.setenv('AMC_ROOT', str(src))
    assert cli._root_dir() == src.resolve()


def test_root_dir_rejects_invalid_amc_root(tmp_path, monkeypatch, capsys):
    bad = tmp_path / 'bad'
    bad.mkdir()
    monkeypatch.setenv('AMC_ROOT', str(bad))
    with pytest.raises(SystemExit) as exc_info:
        cli._root_dir()
    assert exc_info.value.code == 1
    assert 'AMC_ROOT' in capsys.readouterr().err


def test_ensure_venv_install_failure_cleans_partial_venv(tmp_path, monkeypatch, capsys):
    src = _source_root(tmp_path)
    monkeypatch.setenv('AMC_ROOT', str(src))
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[1:3] == ['-m', 'venv']:
            Path(cmd[-1]).mkdir(parents=True)
            return subprocess.CompletedProcess(cmd, 0)
        if '-e' in cmd:
            raise subprocess.CalledProcessError(1, cmd, stderr='network down')
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(cli.subprocess, 'run', fake_run)
    with pytest.raises(SystemExit) as exc_info:
        cli._ensure_venv()
    assert exc_info.value.code == 1
    assert any('-e' in cmd for cmd in calls)
    assert 'Failed to install Python dependencies' in capsys.readouterr().err
    assert not (src / '.venv').exists()


def test_ensure_venv_rejects_incomplete_existing_venv(tmp_path, monkeypatch, capsys):
    src = _source_root(tmp_path)
    monkeypatch.setenv('AMC_ROOT', str(src))
    (src / '.venv').mkdir()
    monkeypatch.setattr(
        cli.subprocess,
        'run',
        lambda *_args, **_kwargs: pytest.fail('must not install over an incomplete venv'),
    )
    with pytest.raises(SystemExit) as exc_info:
        cli._ensure_venv()
    assert exc_info.value.code == 1
    assert 'incomplete' in capsys.readouterr().err


def test_ensure_frontend_deps_failure_cleans_partial_node_modules(tmp_path, monkeypatch, capsys):
    src = _source_root(tmp_path)
    frontend = src / 'frontend'
    frontend.mkdir()
    monkeypatch.setenv('AMC_ROOT', str(src))
    monkeypatch.setattr(cli, '_resolve_npm', lambda: 'npm')

    def fake_run(cmd, **_kwargs):
        (frontend / 'node_modules').mkdir(exist_ok=True)
        raise subprocess.CalledProcessError(1, cmd, stderr='npm failed')

    monkeypatch.setattr(cli.subprocess, 'run', fake_run)
    with pytest.raises(SystemExit) as exc_info:
        cli._ensure_frontend_deps()
    assert exc_info.value.code == 1
    assert 'Failed to install frontend dependencies' in capsys.readouterr().err
    assert not (frontend / 'node_modules').exists()


def test_start_refuses_when_recorded_pid_is_alive(tmp_path, monkeypatch, capsys):
    src = _source_root(tmp_path)
    run_dir = src / '.run'
    run_dir.mkdir()
    (run_dir / 'backend.pid').write_text('12345', encoding='utf-8')
    monkeypatch.setenv('AMC_ROOT', str(src))
    monkeypatch.setattr(cli, '_pid_alive', lambda _pid: True)
    monkeypatch.setattr(
        cli,
        '_ensure_venv',
        lambda: pytest.fail('bootstrap must not run when services are already running'),
    )
    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_start(_start_args())
    assert exc_info.value.code == 1
    assert 'Cannot safely start' in capsys.readouterr().err


def test_start_without_node_or_agent_skips_frontend(tmp_path, monkeypatch, capsys):
    src = _source_root(tmp_path)
    monkeypatch.setenv('AMC_ROOT', str(src))
    monkeypatch.setattr(cli, '_ensure_venv', lambda: None)
    monkeypatch.setattr(cli, '_ensure_frontend_deps', lambda: pytest.fail('frontend must be skipped'))
    monkeypatch.setattr(cli, '_which', lambda _name: None)
    monkeypatch.setattr(cli, '_resolve_npm', lambda: None)
    monkeypatch.setattr(cli, '_detect_backends', lambda: [])
    monkeypatch.setattr(cli, '_find_free_port', lambda port: port)
    monkeypatch.setattr(cli, '_health_ok', lambda _port: True)
    monkeypatch.setattr(cli.signal, 'signal', lambda *_args: None)

    class FakeProc:
        pid = 4242
        returncode = 0

        def poll(self):
            return 0

    started: list[list[str]] = []
    _register_fake_identity(monkeypatch, src, 4242)
    monkeypatch.setattr(cli.subprocess, 'Popen', lambda cmd, **_kwargs: started.append(cmd) or FakeProc())

    cli.cmd_start(_start_args(no_ui=False))
    captured = capsys.readouterr()
    assert 'Node.js not found' in captured.err
    assert 'No supported agent CLI' in captured.err
    assert len(started) == 1
    assert 'uvicorn' in started[0][0]


def test_start_failure_cleans_started_backend(tmp_path, monkeypatch, capsys):
    src = _source_root(tmp_path)
    monkeypatch.setenv('AMC_ROOT', str(src))
    existing_log = src / '.run' / 'backend.log'
    existing_log.parent.mkdir()
    existing_log.write_text('previous diagnostic\n', encoding='utf-8')
    monkeypatch.setattr(cli, '_ensure_venv', lambda: None)
    monkeypatch.setattr(cli, '_which', lambda _name: None)
    monkeypatch.setattr(cli, '_resolve_npm', lambda: None)
    monkeypatch.setattr(cli, '_detect_backends', lambda: ['opencode'])
    monkeypatch.setattr(cli, '_find_free_port', lambda port: port)
    monkeypatch.setattr(cli, '_health_ok', lambda _port: False)
    monkeypatch.setattr(cli, 'HEALTH_TIMEOUT', 1)
    monkeypatch.setattr(cli.time, 'sleep', lambda _seconds: None)
    monkeypatch.setattr(cli.signal, 'signal', lambda *_args: None)
    identity = _register_fake_identity(monkeypatch, src, 4343)
    running = {'value': True}
    monkeypatch.setattr(cli, '_process_identity', lambda pid: identity.copy() if pid == 4343 and running['value'] else None)
    terminated: list[int] = []

    def terminate(item):
        terminated.append(item['pid'])
        running['value'] = False
        return True

    monkeypatch.setattr(cli, '_terminate_service', terminate)

    class FakeProc:
        pid = 4343
        returncode = None

        def poll(self):
            return None

    monkeypatch.setattr(cli.subprocess, 'Popen', lambda *_args, **_kwargs: FakeProc())
    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_start(_start_args())
    assert exc_info.value.code == 1
    assert 4343 in terminated
    assert existing_log.read_text(encoding='utf-8') == 'previous diagnostic\n'
    assert not (src / '.run' / 'backend.json').exists()
    assert 'did not become healthy' in capsys.readouterr().err


def test_frontend_spawn_failure_keeps_both_logs_and_cleans_backend(tmp_path, monkeypatch, capsys):
    src = _source_root(tmp_path)
    monkeypatch.setenv('AMC_ROOT', str(src))
    monkeypatch.setattr(cli, '_ensure_venv', lambda: None)
    monkeypatch.setattr(cli, '_ensure_frontend_deps', lambda: None)
    vite_entry = src / 'frontend' / 'node_modules' / 'vite' / 'bin' / 'vite.js'
    vite_entry.parent.mkdir(parents=True)
    vite_entry.write_text('// fake vite entry', encoding='utf-8')
    monkeypatch.setattr(cli, '_which', lambda name: '/node' if name == 'node' else None)
    monkeypatch.setattr(cli, '_resolve_npm', lambda: '/npm')
    monkeypatch.setattr(cli, '_detect_backends', lambda: ['opencode'])
    monkeypatch.setattr(cli, '_find_free_port', lambda port: port)
    monkeypatch.setattr(cli, '_health_ok', lambda _port: True)
    monkeypatch.setattr(cli, 'run_text', lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout='v22'))
    monkeypatch.setattr(cli.signal, 'signal', lambda *_args: None)
    identity = _register_fake_identity(monkeypatch, src, 4444)
    running = {'value': True}
    monkeypatch.setattr(cli, '_process_identity', lambda pid: identity.copy() if pid == 4444 and running['value'] else None)

    def terminate(_item):
        running['value'] = False
        return True

    monkeypatch.setattr(cli, '_terminate_service', terminate)

    class FakeProc:
        pid = 4444
        returncode = None

        def poll(self):
            return None

    calls = {'count': 0}

    def popen(*_args, **_kwargs):
        calls['count'] += 1
        if calls['count'] == 2:
            raise OSError('node could not start')
        return FakeProc()

    monkeypatch.setattr(cli.subprocess, 'Popen', popen)
    with pytest.raises(SystemExit):
        cli.cmd_start(_start_args(no_ui=False))
    run_dir = src / '.run'
    assert (run_dir / 'backend.log').exists()
    assert (run_dir / 'frontend.log').exists()
    assert not (run_dir / 'backend.json').exists()
    assert 'Check' in capsys.readouterr().err


def test_stop_reports_graceful_shutdown(tmp_path, monkeypatch, capsys):
    src = _source_root(tmp_path)
    run_dir = src / '.run'
    run_dir.mkdir()
    monkeypatch.setenv('AMC_ROOT', str(src))
    alive = {'value': True}
    _write_verified_backend(run_dir, src)
    (run_dir / 'backend.log').write_text('keep diagnostics', encoding='utf-8')
    (run_dir / 'ports.json').write_text('{"backend": 18160, "frontend": null}', encoding='utf-8')
    identity = _register_fake_identity(monkeypatch, src, 12345)
    monkeypatch.setattr(cli, '_process_identity', lambda pid: identity.copy() if pid == 12345 and alive['value'] else None)

    def graceful():
        alive['value'] = False
        return True

    monkeypatch.setattr(cli, '_graceful_shutdown_backend', lambda _item: graceful())
    monkeypatch.setattr(cli, '_port_owner_pid', lambda _port: 12345)
    cli.cmd_stop(SimpleNamespace())
    captured = capsys.readouterr()
    assert 'exited after graceful shutdown' in captured.out
    assert 'All services stopped' in captured.out
    assert not (run_dir / 'backend.json').exists()
    assert not (run_dir / 'backend.pid').exists()
    assert not (run_dir / 'ports.json').exists()


def test_frontend_identity_uses_checkout_vite_entry_after_node_command_expands(tmp_path, monkeypatch):
    src = _source_root(tmp_path)
    monkeypatch.setenv('AMC_ROOT', str(src))
    port, pid = 18161, 54321
    vite_entry = src / 'frontend' / 'node_modules' / 'vite' / 'bin' / 'vite.js'
    vite_entry.parent.mkdir(parents=True)
    vite_entry.write_text('// vite', encoding='utf-8')
    identity = {
        'pid': pid,
        'start_time': 'Sun Oct 26 09:11:00 2025',
        'command': f'node {vite_entry} --host 127.0.0.1 --port {port}',
        'session_id': pid,
        'pgid': pid,
    }
    monkeypatch.setattr(cli, '_process_identity', lambda candidate: identity if candidate == pid else None)
    proc = SimpleNamespace(pid=pid, poll=lambda: None)
    state = cli._register_process('frontend', proc, 'frontend', src, port)
    assert 'npm' not in state['command']
    assert str(vite_entry) in state['signature'][0]
    assert cli._verify_service_state(state)[0]


def test_backend_identity_allows_macos_python_launcher_rewrite(tmp_path, monkeypatch):
    spaced_root = tmp_path / 'repo with spaces'
    spaced_root.mkdir()
    src = _source_root(spaced_root)
    monkeypatch.setenv('AMC_ROOT', str(src))
    pid, port = 54322, 18160
    tail = f'app.api.app:app --app-dir {src / "backend"} --host 127.0.0.1 --port {port}'
    identity = {
        'pid': pid,
        'start_time': 'Sun Oct 26 09:11:00 2025',
        'command': f'/venv path/bin/python3.14 /venv path/bin/uvicorn {tail}',
        'session_id': pid,
        'pgid': pid,
    }
    monkeypatch.setattr(cli, '_process_identity', lambda candidate: identity.copy() if candidate == pid else None)
    proc = SimpleNamespace(pid=pid, poll=lambda: None)
    state = cli._register_process('backend', proc, 'backend', src, port)

    identity['command'] = (
        f'/opt/homebrew/Frameworks/Python.app/Contents/MacOS/Python '
        f'/venv path/bin/uvicorn {tail}'
    )
    assert cli._verify_service_state(state)[0]
    assert cli._service_is_still_same(state) is True

    identity['command'] = identity['command'].replace(str(src / 'backend'), '/different/backend')
    assert not cli._verify_service_state(state)[0]


def test_stop_cleans_matching_stale_state_and_ports(tmp_path, monkeypatch):
    src = _source_root(tmp_path)
    run_dir = src / '.run'
    run_dir.mkdir()
    monkeypatch.setenv('AMC_ROOT', str(src))
    state = _write_verified_backend(run_dir, src)
    (run_dir / 'ports.json').write_text('{"backend": 18160, "frontend": null}', encoding='utf-8')
    monkeypatch.setattr(cli, '_process_identity', lambda _pid: None)
    monkeypatch.setattr(cli, '_pid_alive', lambda _pid: False)
    monkeypatch.setattr(cli, '_port_is_free', lambda _port: True)
    monkeypatch.setattr(cli, '_process_group_alive', lambda _pgid: False)
    cli.cmd_stop(SimpleNamespace())
    assert not (run_dir / 'backend.json').exists()
    assert not (run_dir / 'ports.json').exists()


def test_stop_keeps_leaderless_live_group_state(tmp_path, monkeypatch, capsys):
    src = _source_root(tmp_path)
    run_dir = src / '.run'
    run_dir.mkdir()
    monkeypatch.setenv('AMC_ROOT', str(src))
    _write_verified_backend(run_dir, src)
    (run_dir / 'ports.json').write_text('{"backend": 18160, "frontend": null}', encoding='utf-8')
    monkeypatch.setattr(cli, '_process_identity', lambda _pid: None)
    monkeypatch.setattr(cli, '_pid_alive', lambda _pid: False)
    monkeypatch.setattr(cli, '_port_is_free', lambda _port: True)
    monkeypatch.setattr(cli, '_process_group_alive', lambda _pgid: True)
    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_stop(SimpleNamespace())
    assert exc_info.value.code == 1
    assert (run_dir / 'backend.json').exists()
    assert (run_dir / 'ports.json').exists()
    assert 'process group' in capsys.readouterr().err


def test_stop_waits_for_parent_to_reap_term_exited_group(tmp_path, monkeypatch):
    src = _source_root(tmp_path)
    monkeypatch.setenv('AMC_ROOT', str(src))
    state = _write_verified_backend(src / '.run', src)
    identity = _register_fake_identity(monkeypatch, src, state['pid'])
    queries = {'process': 0, 'group': 0}

    def process_identity(_pid):
        queries['process'] += 1
        return identity.copy() if queries['process'] == 1 else None

    def killpg(_pgid, sig):
        if sig != 0:
            return
        queries['group'] += 1
        if queries['group'] == 1:
            return
        if queries['group'] == 2:
            raise PermissionError
        raise ProcessLookupError

    monkeypatch.setattr(cli, '_process_identity', process_identity)
    monkeypatch.setattr(cli, '_pid_alive', lambda _pid: False)
    monkeypatch.setattr(cli, '_port_is_free', lambda _port: True)
    monkeypatch.setattr(cli.os, 'killpg', killpg, raising=False)
    monkeypatch.setattr(cli.time, 'sleep', lambda _seconds: None)
    item = {'name': 'backend', 'status': 'verified', 'state': state, 'pid': state['pid']}
    assert cli._terminate_service(item)
    assert queries['group'] == 3


def test_stop_waits_for_an_exited_child_to_be_reaped(tmp_path, monkeypatch):
    """A TERM-exited child can briefly have no readable identity but keep its PID."""
    src = _source_root(tmp_path)
    monkeypatch.setenv('AMC_ROOT', str(src))
    state = _write_verified_backend(src / '.run', src)
    identity = _register_fake_identity(monkeypatch, src, state['pid'])
    lookups = iter([identity, None])
    monkeypatch.setattr(cli, '_process_identity', lambda _pid: next(lookups, None))
    monkeypatch.setattr(cli, '_pid_alive', lambda _pid: True)
    monkeypatch.setattr(cli, '_port_is_free', lambda _port: True)
    monkeypatch.setattr(cli.time, 'sleep', lambda _seconds: None)
    signals = []
    checks = {'count': 0}

    def killpg(_pgid, sig):
        signals.append(sig)
        if sig == 0:
            checks['count'] += 1
            if checks['count'] >= 3:
                raise ProcessLookupError

    monkeypatch.setattr(cli.os, 'killpg', killpg, raising=False)
    item = {'name': 'backend', 'status': 'verified', 'state': state, 'pid': state['pid']}
    assert cli._terminate_service(item)
    assert [sig for sig in signals if sig != 0] == [cli.signal.SIGTERM]


def test_frontend_wait_polls_every_child_to_reap_exited_processes():
    polled = []

    class Child:
        def __init__(self, pid, returncode):
            self.pid = pid
            self.returncode = returncode

        def poll(self):
            polled.append(self.pid)
            return self.returncode

    assert not cli._all_processes_exited([Child(1, None), Child(2, 0)])
    assert polled == [1, 2]


def test_windows_process_query_and_taskkill_are_mocked(tmp_path, monkeypatch):
    src = _source_root(tmp_path)
    monkeypatch.setenv('AMC_ROOT', str(src))
    monkeypatch.setattr(cli, 'IS_WINDOWS', True)
    import shutil
    monkeypatch.setattr(shutil, 'which', lambda _name: 'powershell.exe')
    pid, port = 56789, 18160
    command = f'uvicorn app.api.app:app --app-dir {src / "backend"} --port {port}'
    query = subprocess.CompletedProcess([], 0, stdout=(
        '{"CreationDate":"2025-10-26T09:11:00.000000+000",'
        f'"CommandLine":{json.dumps(command)},"ParentProcessId":4321}}'
    ))
    monkeypatch.setattr(cli, 'run_text', lambda *_args, **_kwargs: query)
    identity = cli._process_identity(pid)
    assert identity['parent_pid'] == 4321
    state = {
        'schema': cli.STATE_SCHEMA, 'name': 'backend', 'pid': pid, 'root': str(src.resolve()),
        'port': port, 'signature': cli._service_signature('backend', src.resolve(), port),
        'start_time': identity['start_time'], 'command': identity['command'],
        'session_id': None, 'pgid': None, 'parent_pid': identity['parent_pid'],
    }
    calls = []
    monkeypatch.setattr(cli, 'run_text', lambda cmd, **_kwargs: calls.append(cmd) or (
        query if cmd[0] == 'powershell.exe' else subprocess.CompletedProcess(cmd, 0)
    ))
    assert cli._terminate_service({'name': 'backend', 'status': 'verified', 'state': state, 'pid': pid})
    assert calls[1][:4] == ['taskkill', '/F', '/T', '/PID']


def test_stop_signals_only_verified_record_and_preserves_unrelated_files(tmp_path, monkeypatch):
    src = _source_root(tmp_path)
    run_dir = src / '.run'
    run_dir.mkdir()
    monkeypatch.setenv('AMC_ROOT', str(src))
    _write_verified_backend(run_dir, src)
    (run_dir / 'backend.log').write_text('keep diagnostics', encoding='utf-8')
    (run_dir / 'ports.json').write_text('{"backend": 18161, "frontend": null}', encoding='utf-8')
    unknown = run_dir / 'keep-me.txt'
    unknown.write_text('user data', encoding='utf-8')
    alive = {'value': True}
    identity = _register_fake_identity(monkeypatch, src, 12345)
    monkeypatch.setattr(cli, '_process_identity', lambda pid: identity.copy() if pid == 12345 and alive['value'] else None)
    signalled: list[int] = []

    def terminate(item):
        signalled.append(item['pid'])
        alive['value'] = False
        return True

    monkeypatch.setattr(cli, '_terminate_service', terminate)
    monkeypatch.setattr(cli, '_port_owner_pid', lambda _port: None)
    cli.cmd_stop(SimpleNamespace())
    assert signalled == [12345]
    assert unknown.read_text(encoding='utf-8') == 'user data'
    assert (run_dir / 'backend.log').read_text(encoding='utf-8') == 'keep diagnostics'
    assert not (run_dir / 'backend.json').exists()
    assert (run_dir / 'ports.json').read_text(encoding='utf-8') == '{"backend": 18161, "frontend": null}'


def test_bad_or_empty_state_never_signals_pid(tmp_path, monkeypatch, capsys):
    src = _source_root(tmp_path)
    run_dir = src / '.run'
    run_dir.mkdir()
    monkeypatch.setenv('AMC_ROOT', str(src))
    (run_dir / 'backend.pid').write_text('12345', encoding='utf-8')
    (run_dir / 'backend.json').write_text('{"schema": 2, "pid": 12345, "signature": []}', encoding='utf-8')
    monkeypatch.setattr(cli, '_process_identity', lambda _pid: pytest.fail('corrupt state must not query or signal'))
    monkeypatch.setattr(cli, '_terminate_service', lambda _item: pytest.fail('must refuse'))
    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_stop(SimpleNamespace())
    assert exc_info.value.code == 1
    assert (run_dir / 'backend.json').exists()
    assert 'state kept' in capsys.readouterr().err


def test_graceful_shutdown_refuses_stale_port_owner(tmp_path, monkeypatch):
    src = _source_root(tmp_path)
    run_dir = src / '.run'
    run_dir.mkdir()
    monkeypatch.setenv('AMC_ROOT', str(src))
    state = _write_verified_backend(run_dir, src)
    (run_dir / 'ports.json').write_text('{"backend": 18160}', encoding='utf-8')
    identity = _register_fake_identity(monkeypatch, src, state['pid'])
    monkeypatch.setattr(cli, '_process_identity', lambda _pid: identity.copy())
    monkeypatch.setattr(cli, '_port_owner_pid', lambda _port: 99999)
    monkeypatch.setattr(cli, 'run_text', lambda *_args, **_kwargs: pytest.fail('must not issue HTTP request'))
    assert not cli._graceful_shutdown_backend({'name': 'backend', 'status': 'verified', 'state': state})


def test_identity_query_timeout_is_unavailable(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise cli.subprocess.TimeoutExpired('ps', 5)

    monkeypatch.setattr(cli, 'run_text', timeout)
    assert cli._process_identity(12345) is None


def test_term_does_not_escalate_after_pid_identity_changes(tmp_path, monkeypatch):
    src = _source_root(tmp_path)
    monkeypatch.setenv('AMC_ROOT', str(src))
    state = _write_verified_backend(src / '.run', src)
    original = _register_fake_identity(monkeypatch, src, state['pid'])
    changed = {**original, 'start_time': 'Sun Oct 26 10:00:00 2025'}
    calls = {'count': 0}

    def identities(_pid):
        calls['count'] += 1
        return original.copy() if calls['count'] == 1 else changed.copy()

    monkeypatch.setattr(cli, '_process_identity', identities)
    signals: list[int] = []
    monkeypatch.setattr(cli.os, 'killpg', lambda _pgid, sig: signals.append(sig), raising=False)
    monkeypatch.setattr(cli.time, 'sleep', lambda _seconds: None)
    item = {'name': 'backend', 'status': 'verified', 'state': state, 'pid': state['pid'], 'reason': ''}
    assert not cli._terminate_service(item)
    assert [sig for sig in signals if sig != 0] == [cli.signal.SIGTERM]


def test_verified_process_gets_term_then_kill_after_rechecks(tmp_path, monkeypatch):
    src = _source_root(tmp_path)
    monkeypatch.setenv('AMC_ROOT', str(src))
    state = _write_verified_backend(src / '.run', src)
    identity = _register_fake_identity(monkeypatch, src, state['pid'])
    monkeypatch.setattr(cli, '_process_identity', lambda _pid: identity.copy())
    monkeypatch.setattr(cli.time, 'sleep', lambda _seconds: None)
    signals: list[int] = []
    group = {'alive': True}

    def killpg(_pgid, sig):
        signals.append(sig)
        if sig == cli.signal.SIGKILL:
            group['alive'] = False
        elif sig == 0 and not group['alive']:
            raise ProcessLookupError

    monkeypatch.setattr(cli.os, 'killpg', killpg, raising=False)
    item = {'name': 'backend', 'status': 'verified', 'state': state, 'pid': state['pid'], 'reason': ''}
    assert cli._terminate_service(item)
    assert cli.signal.SIGTERM in signals
    assert cli.signal.SIGKILL in signals
    assert signals.count(0) >= 11


def test_stop_refuses_state_from_another_checkout(tmp_path, monkeypatch, capsys):
    src = _source_root(tmp_path)
    (tmp_path / 'other').mkdir()
    other = _source_root(tmp_path / 'other')
    run_dir = src / '.run'
    run_dir.mkdir()
    monkeypatch.setenv('AMC_ROOT', str(src))
    state = _write_verified_backend(run_dir, other)
    monkeypatch.setattr(cli, '_process_identity', lambda _pid: pytest.fail('root mismatch must reject before lookup'))
    monkeypatch.setattr(cli, '_terminate_service', lambda _item: pytest.fail('root mismatch must not signal'))
    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_stop(SimpleNamespace())
    assert exc_info.value.code == 1
    assert (run_dir / 'backend.json').exists()
    assert 'state kept' in capsys.readouterr().err


def test_dead_service_with_occupied_port_is_kept_unverified(tmp_path, monkeypatch, capsys):
    src = _source_root(tmp_path)
    run_dir = src / '.run'
    run_dir.mkdir()
    monkeypatch.setenv('AMC_ROOT', str(src))
    _write_verified_backend(run_dir, src)
    monkeypatch.setattr(cli, '_process_identity', lambda _pid: None)
    monkeypatch.setattr(cli, '_pid_alive', lambda _pid: False)
    monkeypatch.setattr(cli, '_port_is_free', lambda _port: False)
    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_stop(SimpleNamespace())
    assert exc_info.value.code == 1
    assert (run_dir / 'backend.json').exists()
    assert 'ownership is unknown' in capsys.readouterr().err
