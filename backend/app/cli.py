"""Cross-platform CLI for Mission Control.

Usage:
    amc start [--port PORT] [--ui-port PORT] [--no-ui] [--no-browser]
    amc stop
    amc status
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from app.core.proc import run_text

RUN_DIR_NAME = '.run'
BACKEND_DEFAULT_PORT = 8000
FRONTEND_DEFAULT_PORT = 5173
HEALTH_TIMEOUT = 30
STATE_SCHEMA = 2
SERVICE_NAMES = ('backend', 'frontend')

IS_WINDOWS = sys.platform == 'win32'


def _enable_windows_vt_mode() -> bool:
    """Try to enable ANSI escape support in the current Windows console.

    Returns True if VT processing is active (or not needed). On older Windows
    builds where this fails, callers should strip ANSI / use ASCII fallbacks.
    """
    if not IS_WINDOWS:
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        STD_OUTPUT_HANDLE = -11
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        new_mode = mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(kernel32.SetConsoleMode(handle, new_mode))
    except Exception:
        return False


def _try_set_utf8_codepage() -> None:
    """Best-effort: switch the active Windows console codepage to UTF-8 (65001)."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass


_USE_COLOR = (not IS_WINDOWS) or _enable_windows_vt_mode()
if IS_WINDOWS:
    _try_set_utf8_codepage()
# Honor explicit overrides for environments without TTY support.
if os.environ.get('NO_COLOR'):
    _USE_COLOR = False
if os.environ.get('AMC_FORCE_COLOR'):
    _USE_COLOR = True

_SYM_OK = '[OK]'
_SYM_WARN = '[!]'
_SYM_FAIL = '[x]'


def _color(code: str, msg: str) -> str:
    if not _USE_COLOR:
        return msg
    return f'\033[{code}m{msg}\033[0m'


def _root_dir() -> Path:
    """Resolve the source checkout used for .venv, frontend and .run."""
    override = os.environ.get('AMC_ROOT', '').strip()
    if override:
        root = Path(override).expanduser().resolve()
        if not (root / 'pyproject.toml').exists():
            _fail(f'AMC_ROOT does not point to a Mission Control source checkout: {root}')
        return root
    here = Path(__file__).resolve().parent
    for ancestor in [here, here.parent, here.parent.parent]:
        if (ancestor / 'pyproject.toml').exists():
            return ancestor
    return here.parent


def _run_dir() -> Path:
    return _root_dir() / RUN_DIR_NAME


def _venv_python() -> Path:
    root = _root_dir()
    if IS_WINDOWS:
        return root / '.venv' / 'Scripts' / 'python.exe'
    return root / '.venv' / 'bin' / 'python'


def _venv_bin(name: str) -> Path:
    root = _root_dir()
    if IS_WINDOWS:
        return root / '.venv' / 'Scripts' / (name + '.exe')
    return root / '.venv' / 'bin' / name


# ── helpers ───────────────────────────────────────────────────────────

def _info(msg: str) -> None:
    print(_color('0;36', msg), flush=True)


def _eprint(msg: str = '') -> None:
    print(msg, file=sys.stderr, flush=True)


def _print_json(payload: dict) -> None:
    """Write machine-readable output as UTF-8 even when the console codepage differs."""
    text = json.dumps(payload, indent=2, ensure_ascii=False) + '\n'
    stream = getattr(sys.stdout, 'buffer', None)
    if stream is not None:
        stream.write(text.encode('utf-8'))
        stream.flush()
    else:
        sys.stdout.write(text)
        sys.stdout.flush()


def _review_error(
    message: str,
    *,
    code: str,
    output_format: str,
    hints: list[str] | tuple[str, ...] = (),
    recoverable: bool = False,
) -> None:
    """Report a review failure on stderr and keep JSON stdout parseable."""
    _eprint(f'{_SYM_FAIL} {message}')
    for hint in hints:
        _eprint(f'    {hint}')
    if output_format == 'json':
        summary = ' '.join([message, *hints])
        _print_json({
            'task_id': '',
            'status': 'error',
            'verdict': 'failed',
            'passed': False,
            'finding_count': 0,
            'current_round_finding_count': 0,
            'findings': [],
            'summary': summary,
            'rounds_executed': 0,
            'duration_sec': 0.0,
            'can_continue': False,
            'error': {'code': code, 'message': summary, 'recoverable': recoverable},
        })
    sys.exit(2)


def _no_changes_payload(base: str | None, message: str) -> dict:
    return {
        'task_id': '',
        'status': 'no_changes',
        'verdict': 'inconclusive',
        'passed': True,
        'finding_count': 0,
        'current_round_finding_count': 0,
        'findings': [],
        'summary': message,
        'rounds_executed': 0,
        'duration_sec': 0.0,
        'can_continue': False,
        'base': base,
    }


def _ok(msg: str) -> None:
    print(_color('0;32', f'{_SYM_OK} {msg}'), flush=True)


def _warn(msg: str) -> None:
    print(_color('0;33', f'{_SYM_WARN} {msg}'), file=sys.stderr, flush=True)


def _fail(msg: str) -> None:
    print(_color('0;31', f'{_SYM_FAIL} {msg}'), file=sys.stderr, flush=True)
    sys.exit(1)


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) != 0


def _find_free_port(preferred: int) -> int:
    port = preferred
    while port < preferred + 20:
        if _port_is_free(port):
            return port
        port += 1
    _fail(f'Could not find a free port near {preferred}')
    return preferred  # unreachable


def _which(name: str) -> str | None:
    import shutil
    return shutil.which(name)


def _save_pid(name: str, pid: int) -> None:
    run_dir = _run_dir()
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / f'{name}.pid').write_text(str(pid))


def _read_pid(name: str) -> int | None:
    pidfile = _run_dir() / f'{name}.pid'
    if pidfile.exists():
        try:
            return int(pidfile.read_text().strip())
        except (OSError, ValueError):
            return None
    return None


def _state_path(name: str) -> Path:
    return _run_dir() / f'{name}.json'


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            _warn(f'Could not remove {path}: {exc}')


def _read_json_file(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _process_identity(pid: int) -> dict | None:
    if pid <= 0:
        return None
    if IS_WINDOWS:
        return _windows_process_identity(pid)
    env = os.environ.copy()
    env['LC_ALL'] = 'C'
    try:
        result = run_text(
            ['ps', '-p', str(pid), '-o', 'lstart=', '-o', 'command='],
            env=env,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    match = re.match(r'^(.{24})\s+(.*)$', (result.stdout or '').rstrip('\n'))
    if not match or not match.group(2).strip():
        return None
    start_time, command = match.groups()
    try:
        session_id = os.getsid(pid)
        pgid = os.getpgid(pid)
    except (OSError, ProcessLookupError):
        return None
    return {
        'pid': pid,
        'start_time': start_time,
        'command': command,
        'session_id': session_id,
        'pgid': pgid,
        'source': 'ps',
    }


def _windows_process_identity(pid: int) -> dict | None:
    import shutil
    executable = shutil.which('powershell') or shutil.which('pwsh')
    if not executable:
        return None
    script = (
        '[Console]::OutputEncoding=[Text.Encoding]::UTF8; '
        f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}'; "
        'if ($p) { [pscustomobject]@{CreationDate=$p.CreationDate.ToString("o");'
        'CommandLine=$p.CommandLine;ParentProcessId=$p.ParentProcessId} | ConvertTo-Json -Compress }'
    )
    try:
        result = run_text([executable, '-NoProfile', '-NonInteractive', '-Command', script], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not (result.stdout or '').strip():
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return {
        'pid': pid,
        'start_time': str(data.get('CreationDate') or ''),
        'command': str(data.get('CommandLine') or ''),
        'session_id': None,
        'pgid': None,
        'source': 'cim',
        'parent_pid': data.get('ParentProcessId'),
    }


def _save_service_state(name: str, state: dict) -> None:
    _atomic_write_json(_state_path(name), state)
    _save_pid(name, int(state['pid']))


def _load_service_state(name: str) -> tuple[dict | None, str]:
    path = _state_path(name)
    if not path.exists():
        return None, 'missing'
    data = _read_json_file(path)
    if data is None or type(data.get('schema')) is not int or data.get('schema') != STATE_SCHEMA:
        return None, 'corrupt'
    pid = data.get('pid')
    signature = data.get('signature')
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(data.get('root'), str)
        or not data['root'].strip()
        or data.get('name') != name
        or not isinstance(data.get('start_time'), str)
        or not data['start_time']
        or not isinstance(data.get('command'), str)
        or not data['command'].strip()
        or not isinstance(data.get('port'), int)
        or isinstance(data.get('port'), bool)
        or not 1 <= data['port'] <= 65535
        or not isinstance(signature, list)
        or not signature
        or not all(isinstance(item, str) and item for item in signature)
        or (not IS_WINDOWS and (type(data.get('session_id')) is not int or type(data.get('pgid')) is not int))
        or (IS_WINDOWS and (type(data.get('parent_pid')) is not int or data['parent_pid'] <= 0))
    ):
        return None, 'corrupt'
    return data, ''


def _remove_service_artifacts(name: str) -> None:
    for path in (_state_path(name), _run_dir() / f'{name}.pid'):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            _warn(f'Could not remove {path}: {exc}')


def _remove_ports_file() -> None:
    try:
        (_run_dir() / 'ports.json').unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        _warn(f'Could not remove {_run_dir() / "ports.json"}: {exc}')


def _ports_file_matches_services(items: list[dict]) -> bool:
    ports = _read_json_file(_run_dir() / 'ports.json')
    if ports is None:
        return False
    by_name = {item['name']: item for item in items}
    matched = False
    for name, port in ports.items():
        if name not in SERVICE_NAMES:
            return False
        if port is None:
            continue
        item = by_name.get(name)
        if not item or not isinstance(item.get('state'), dict) or item['state'].get('port') != port:
            return False
        matched = True
    return matched


def _service_signature(kind: str, root: Path, port: int) -> list[str]:
    if kind == 'backend':
        return ['app.api.app:app', '--app-dir', str(root / 'backend'), '--port', str(port)]
    return [str(root / 'frontend' / 'node_modules' / 'vite' / 'bin' / 'vite.js'), '--port', str(port)]


def _command_fingerprint(command: str, signature: list[str]) -> str | None:
    """Ignore a launcher prefix while keeping the service entrypoint and argv tail exact."""
    if not isinstance(command, str) or not signature:
        return None
    marker = signature[0]
    index = command.find(marker)
    return command[index:] if index >= 0 else None


def _register_process(name: str, proc: subprocess.Popen, kind: str, root: Path, port: int) -> dict:
    """Persist a verifiable process fingerprint; a PID alone is never ownership proof."""
    identity = None
    for _ in range(20):
        identity = _process_identity(proc.pid)
        if identity is not None:
            break
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    if identity is None:
        raise RuntimeError(f'Cannot verify {name} process identity (PID {proc.pid}); refusing to manage it by PID alone.')
    signature = _service_signature(kind, root.resolve(), port)
    fingerprint = _command_fingerprint(identity.get('command'), signature)
    if fingerprint is None or any(token not in fingerprint for token in signature):
        raise RuntimeError(f'Cannot verify {name} command line (PID {proc.pid}); refusing to manage it by PID alone.')
    if not identity.get('start_time'):
        raise RuntimeError(f'Cannot verify {name} start time (PID {proc.pid}); refusing to manage it by PID alone.')
    if not IS_WINDOWS and (
        identity.get('session_id') != proc.pid or identity.get('pgid') != proc.pid
    ):
        raise RuntimeError(f'Cannot verify isolated {name} process group (PID {proc.pid}); refusing group signals.')
    if IS_WINDOWS and (type(identity.get('parent_pid')) is not int or identity['parent_pid'] <= 0):
        raise RuntimeError(f'Cannot verify {name} parent process (PID {proc.pid}); refusing to manage it by PID alone.')
    state = {
        'schema': STATE_SCHEMA,
        'name': name,
        'pid': proc.pid,
        'root': str(root.resolve()),
        'port': port,
        'signature': signature,
        'start_time': identity.get('start_time'),
        'command': identity.get('command'),
        'session_id': identity.get('session_id'),
        'pgid': identity.get('pgid'),
        'parent_pid': identity.get('parent_pid'),
    }
    if not state['start_time'] or not state['command']:
        raise RuntimeError(f'Cannot verify {name} process identity (PID {proc.pid}); refusing to manage it by PID alone.')
    _save_service_state(name, state)
    return state


def _verify_service_state(state: dict) -> tuple[bool, dict | None, str]:
    try:
        state_root = Path(str(state.get('root') or '')).resolve()
    except (OSError, RuntimeError, ValueError):
        return False, None, 'invalid root in state'
    if state_root != _root_dir().resolve():
        return False, None, 'state root does not match this checkout'
    name = state.get('name')
    port = state.get('port')
    if name not in SERVICE_NAMES or not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        return False, None, 'invalid service name or port in state'
    if state.get('signature') != _service_signature(name, state_root, port):
        return False, None, 'service signature does not match this checkout and port'
    pid = state.get('pid')
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False, None, 'invalid pid in state'
    identity = _process_identity(pid)
    if identity is None:
        return False, None, 'process identity is unavailable'
    if state.get('start_time') != identity.get('start_time'):
        return False, identity, 'start time mismatch (PID may have been reused)'
    signature = state.get('signature')
    stored_fingerprint = _command_fingerprint(state.get('command'), signature)
    current_fingerprint = _command_fingerprint(identity.get('command'), signature)
    if stored_fingerprint is None or current_fingerprint != stored_fingerprint:
        return False, identity, 'service command changed since startup'
    if not IS_WINDOWS:
        if state.get('session_id') != identity.get('session_id'):
            return False, identity, 'session id mismatch'
        if state.get('pgid') != identity.get('pgid'):
            return False, identity, 'process group mismatch'
    elif state.get('parent_pid') is not None and state.get('parent_pid') != identity.get('parent_pid'):
        return False, identity, 'parent process mismatch'
    command = current_fingerprint or ''
    missing = [token for token in state.get('signature') or [] if token not in command]
    if missing:
        return False, identity, f'command does not match expected service (missing {missing[0]!r})'
    return True, identity, ''


def _inspect_services() -> list[dict]:
    run_dir = _run_dir()
    if not run_dir.exists():
        return []
    items: list[dict] = []
    for name in SERVICE_NAMES:
        state, error = _load_service_state(name)
        pid_path = run_dir / f'{name}.pid'
        if state is None:
            if error == 'corrupt':
                items.append({
                    'name': name,
                    'status': 'corrupt',
                    'state': None,
                    'pid': None,
                    'identity': None,
                    'reason': 'state file is corrupt or unreadable',
                })
            elif pid_path.exists():
                pid = _read_pid(name)
                if pid is None:
                    items.append({
                        'name': name,
                        'status': 'corrupt',
                        'state': None,
                        'pid': None,
                        'identity': None,
                        'reason': 'pid file is unreadable',
                    })
                elif _pid_alive(pid):
                    items.append({
                        'name': name,
                        'status': 'legacy',
                        'state': None,
                        'pid': pid,
                        'identity': None,
                        'reason': 'pid file has no ownership state',
                    })
                else:
                    items.append({
                        'name': name,
                        'status': 'stale',
                        'state': None,
                        'pid': pid,
                        'identity': None,
                        'reason': 'recorded pid is not running',
                    })
            continue
        ok, identity, reason = _verify_service_state(state)
        pid = state['pid']
        if ok:
            items.append({
                'name': name,
                'status': 'verified',
                'state': state,
                'pid': pid,
                'identity': identity,
                'reason': '',
            })
        elif identity is None and reason == 'process identity is unavailable' and not _pid_alive(pid):
            group_alive = _process_group_alive(state['pgid']) if not IS_WINDOWS else False
            if group_alive is True or group_alive is None:
                items.append({
                    'name': name,
                    'status': 'unverified',
                    'state': state,
                    'pid': pid,
                    'identity': None,
                    'reason': 'recorded leader exited but process group is still present or cannot be checked',
                })
            elif _port_is_free(state['port']):
                items.append({
                    'name': name,
                    'status': 'stale',
                    'state': state,
                    'pid': pid,
                    'identity': None,
                    'reason': 'recorded process is not running and its port is free',
                })
            else:
                items.append({
                    'name': name,
                    'status': 'unverified',
                    'state': state,
                    'pid': pid,
                    'identity': None,
                    'reason': 'recorded process exited but its port still has a listener; ownership is unknown',
                })
        else:
            items.append({
                'name': name,
                'status': 'unverified',
                'state': state,
                'pid': pid,
                'identity': identity,
                'reason': reason,
            })
    return items


def _issue_text(item: dict) -> str:
    suffix = f' (PID {item["pid"]})' if item.get('pid') else ''
    return f'{item["name"]}{suffix}: {item["reason"]}'


def _pid_alive(pid: int) -> bool:
    try:
        if IS_WINDOWS:
            result = run_text(['tasklist', '/FI', f'PID eq {pid}', '/NH'])
            return str(pid) in (result.stdout or '')
        else:
            os.kill(pid, 0)
            return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, subprocess.SubprocessError):
        return True


def _service_is_still_same(state: dict) -> bool | None:
    """True means same process; False means PID is gone; None means identity changed or is unknown."""
    identity = _process_identity(state['pid'])
    if identity is not None:
        signature = state.get('signature') or []
        same = (
            identity.get('start_time') == state.get('start_time')
            and _command_fingerprint(identity.get('command'), signature)
            == _command_fingerprint(state.get('command'), signature)
            and _command_fingerprint(state.get('command'), signature) is not None
        )
        return True if same else None
    return None if _pid_alive(state['pid']) else False


def _process_group_alive(pgid: int) -> bool | None:
    if not hasattr(os, 'killpg'):
        return None
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None


def _service_stopped(state: dict) -> bool:
    if _service_is_still_same(state) is not False:
        return False
    if not IS_WINDOWS and _process_group_alive(state.get('pgid')) is not False:
        return False
    try:
        return _port_is_free(state['port'])
    except OSError:
        return False


def _port_owner_pid(port: int) -> int | None:
    """Return the confirmed local TCP listener PID, or None when unavailable."""
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        return None
    try:
        if IS_WINDOWS:
            import shutil
            executable = shutil.which('powershell') or shutil.which('pwsh')
            if not executable:
                return None
            script = (
                f"$c=Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction Stop; "
                "if ($c) { ($c | Select-Object -First 1 -ExpandProperty OwningProcess) }"
            )
            result = run_text([executable, '-NoProfile', '-NonInteractive', '-Command', script], timeout=5)
            if result.returncode != 0:
                return None
            value = (result.stdout or '').strip()
            return int(value) if value.isdigit() else None
        else:
            import shutil
            executable = shutil.which('lsof')
            if not executable:
                return None
            result = run_text(
                [executable, '-nP', f'-iTCP:{port}', '-sTCP:LISTEN', '-t'], timeout=5
            )
            if result.returncode not in (0, 1):
                return None
            pids = [line.strip() for line in (result.stdout or '').splitlines() if line.strip().isdigit()]
            return int(pids[0]) if len(pids) == 1 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _terminate_service(item: dict) -> bool:
    """Signal only a service whose persisted identity still matches at each step."""
    if item.get('status') != 'verified' or not isinstance(item.get('state'), dict):
        _warn(f'{_issue_text(item)}; refusing to signal it. Inspect the process manually.')
        return False
    state = item['state']
    pid = state['pid']
    valid, identity, reason = _verify_service_state(state)
    if not valid or identity is None:
        _warn(f'{item["name"]} (PID {pid}): {reason}; refusing to signal it. Inspect the process manually.')
        return False
    if IS_WINDOWS:
        try:
            result = run_text(['taskkill', '/F', '/T', '/PID', str(pid)], timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            _warn(f'Could not verify/stop {item["name"]} (PID {pid}) on Windows: {exc}; state kept.')
            return False
        if result.returncode != 0:
            _warn(f'Windows taskkill could not stop {item["name"]} (PID {pid}); state kept. Inspect it manually.')
            return False
        return True

    session_id, pgid = state.get('session_id'), state.get('pgid')
    if session_id != pid or pgid != pid or identity.get('session_id') != pid or identity.get('pgid') != pid:
        _warn(f'{item["name"]} (PID {pid}): process group is not an isolated AMC session; refusing to signal it.')
        return False
    group_alive = _process_group_alive(pgid)
    if group_alive is False:
        return _service_stopped(state)
    if group_alive is None:
        _warn(f'Could not verify {item["name"]} process group (PID {pid}); state kept.')
        return False
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return _service_stopped(state)
    except OSError as exc:
        _warn(f'Could not stop {item["name"]} (PID {pid}): {exc}')
        return False

    for _ in range(10):
        time.sleep(0.5)
        group_alive = _process_group_alive(pgid)
        if group_alive is False:
            if _port_is_free(state['port']):
                return True
            continue
        if group_alive is None:
            _warn(f'Could not verify {item["name"]} process group after SIGTERM; state kept.')
            return False
        still_same = _service_is_still_same(state)
        if still_same is False:
            if group_alive is True:
                continue
            if not _port_is_free(state['port']):
                continue
            return True
        if still_same is None:
            # A TERM-exited child can briefly be a zombie until its parent
            # reaps it. Wait without sending another signal; escalation below
            # still requires a fresh, complete identity match.
            continue

    # Re-read the complete fingerprint immediately before the destructive signal.
    valid, current, reason = _verify_service_state(state)
    if not valid or current is None:
        _warn(f'{item["name"]} (PID {pid}): identity no longer verified ({reason}); refusing SIGKILL.')
        return False
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return _service_stopped(state)
    except OSError as exc:
        _warn(f'Could not force-stop {item["name"]} (PID {pid}): {exc}')
        return False
    for _ in range(10):
        time.sleep(0.5)
        group_alive = _process_group_alive(pgid)
        if group_alive is False and _port_is_free(state['port']):
            return True
        if group_alive is None:
            break
    _warn(f'{item["name"]} process group remains or cannot be checked after SIGKILL; state kept.')
    return False


def _health_ok(port: int) -> bool:
    try:
        import urllib.request
        resp = urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=2)
        return resp.status == 200
    except Exception:
        return False


# ── bootstrap ─────────────────────────────────────────────────────────

def _ensure_venv() -> None:
    root = _root_dir()
    venv_dir = root / '.venv'
    if venv_dir.exists():
        if not _venv_bin('uvicorn').exists():
            _fail(
                f'Existing virtual environment is incomplete (uvicorn missing): {venv_dir}. '
                'Remove it and re-run amc start, or install the project into it with pip install -e .'
            )
        _ok('Using existing virtual environment')
        return

    _info('First run — setting up virtual environment...')
    python = sys.executable
    try:
        subprocess.run([python, '-m', 'venv', str(venv_dir)], check=True)
        vpy = str(_venv_python())
        subprocess.run([vpy, '-m', 'pip', 'install', '--quiet', '--upgrade', 'pip'], check=True)
        subprocess.run([vpy, '-m', 'pip', 'install', '--quiet', '-e', str(root)], check=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        import shutil
        shutil.rmtree(venv_dir, ignore_errors=True)
        _fail(
            f'Failed to install Python dependencies in {venv_dir}: {exc}. '
            'Check network/PyPI access, then re-run amc start.'
        )
    if not _venv_bin('uvicorn').exists():
        import shutil
        shutil.rmtree(venv_dir, ignore_errors=True)
        _fail(f'Python environment is incomplete after installation (uvicorn missing): {venv_dir}')
    _ok('Python environment ready')


def _ensure_frontend_deps() -> None:
    root = _root_dir()
    node_modules = root / 'frontend' / 'node_modules'
    if node_modules.exists():
        return
    npm_bin = _resolve_npm()
    if not npm_bin:
        return
    _info('Installing frontend dependencies...')
    try:
        subprocess.run(
            [npm_bin, 'install', '--silent'],
            cwd=str(root / 'frontend'),
            check=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        import shutil
        shutil.rmtree(node_modules, ignore_errors=True)
        _fail(
            f'Failed to install frontend dependencies in {root / "frontend"}: {exc}. '
            'Check npm/network access, or run amc start --no-ui.'
        )
    _ok('Frontend dependencies ready')


def _resolve_npm() -> str | None:
    """Locate the npm launcher cross-platform.

    Windows installs npm as ``npm.cmd``; ``shutil.which('npm')`` resolves it
    correctly when available. Falling back to a bare name keeps macOS/Linux
    behavior unchanged when PATH lookup is denied (sandboxed runs, etc.).
    """
    import shutil
    resolved = shutil.which('npm')
    if resolved:
        return resolved
    if IS_WINDOWS:
        resolved = shutil.which('npm.cmd')
        if resolved:
            return resolved
    return None


# ── commands ──────────────────────────────────────────────────────────

def cmd_start(args: argparse.Namespace) -> None:
    root = _root_dir()
    existing = _inspect_services()
    blockers = [item for item in existing if item['status'] in {'verified', 'unverified', 'legacy', 'corrupt'}]
    if blockers:
        details = '; '.join(_issue_text(item) for item in blockers)
        _fail(f'Cannot safely start Mission Control: {details}. Run `amc status` and inspect the process/state manually.')
    ports_path = _run_dir() / 'ports.json'
    stale = [item for item in existing if item['status'] == 'stale']
    if ports_path.exists() and stale and not _ports_file_matches_services(existing):
        _fail(f'Found {ports_path} that does not match the recorded stale services. It was preserved; inspect it manually before starting.')
    if ports_path.exists() and not stale:
        _fail(f'Found pre-existing {ports_path} with no registered service. It was preserved; inspect/remove it manually before starting.')
    for item in stale:
        _remove_service_artifacts(item['name'])
    if stale and _ports_file_matches_services(existing):
        _remove_ports_file()

    # prerequisites
    _info('Checking prerequisites...')

    python_ver = f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}'
    if sys.version_info < (3, 11):
        _fail(f'Python 3.11+ required, found {python_ver}')
    _ok(f'Python: {python_ver}')

    node_bin = _which('node')
    has_node = bool(node_bin)
    if has_node:
        node_ver = (run_text([node_bin, '--version']).stdout or '').strip()
        _ok(f'Node.js: {node_ver}')
    else:
        _warn('Node.js not found. Frontend will not be started.')
    has_npm = bool(_resolve_npm()) if has_node else False
    if has_node and not has_npm:
        _warn('npm not found. Frontend will not be started.')

    detected_backends = _detect_backends()
    if detected_backends:
        _ok(f'Agent backends: {", ".join(detected_backends)}')
    else:
        _warn('No supported agent CLI found on PATH. Reviews will fail until one is installed.')

    gh_token = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
    if not gh_token:
        _warn('No GITHUB_TOKEN or GH_TOKEN set. GitHub API rate limits will apply.')

    # bootstrap
    _ensure_venv()
    skip_ui = args.no_ui or not has_node or not has_npm
    if not skip_ui:
        _ensure_frontend_deps()

    # find ports
    backend_port = _find_free_port(args.port)
    if backend_port != args.port:
        _info(f'Port {args.port} in use, using {backend_port} for backend')

    frontend_port = None
    if not skip_ui:
        frontend_port = _find_free_port(args.ui_port)
        if frontend_port != args.ui_port:
            _info(f'Port {args.ui_port} in use, using {frontend_port} for frontend')

    started: list[dict] = []
    frontend_proc = None
    try:
        # start backend
        _info(f'Starting backend on port {backend_port}...')
        uvicorn_bin = str(_venv_bin('uvicorn'))
        backend_log = _run_dir() / 'backend.log'
        _run_dir().mkdir(parents=True, exist_ok=True)

        backend_cmd = [
            uvicorn_bin, 'app.api.app:app',
            '--app-dir', str(root / 'backend'),
            '--host', '127.0.0.1',
            '--port', str(backend_port),
        ]
        # --reload spawns a separate reloader+worker pair, which makes process
        # cleanup unreliable (especially on Windows). Allow opt-in for dev only.
        if os.environ.get('AMC_DEV_RELOAD'):
            backend_cmd.append('--reload')
        try:
            with open(backend_log, 'a', encoding='utf-8', errors='replace', newline='') as log:
                backend_proc = subprocess.Popen(
                    backend_cmd,
                    stdout=log, stderr=log,
                    start_new_session=not IS_WINDOWS,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0,
                )
        except OSError as exc:
            _fail(f'Failed to start backend: {exc}. Check {backend_log}.')
        backend_started = {'name': 'backend', 'pid': backend_proc.pid, 'proc': backend_proc, 'state': None}
        started.append(backend_started)
        try:
            backend_started['state'] = _register_process('backend', backend_proc, 'backend', root, backend_port)
        except RuntimeError as exc:
            _fail(f'{exc} Check {backend_log}; if it remains active, inspect PID {backend_proc.pid} manually.')

        # start frontend
        if not skip_ui and frontend_port is not None:
            _info(f'Starting frontend on port {frontend_port}...')
            frontend_log = _run_dir() / 'frontend.log'
            env = os.environ.copy()
            env['VITE_API_TARGET'] = f'http://127.0.0.1:{backend_port}'
            vite_entry = root / 'frontend' / 'node_modules' / 'vite' / 'bin' / 'vite.js'
            if not vite_entry.is_file():
                _fail(f'Vite entry point is missing: {vite_entry}. Run npm install in frontend/.')
            frontend_cmd = [node_bin, str(vite_entry), '--host', '127.0.0.1', '--port', str(frontend_port)]
            try:
                with open(frontend_log, 'a', encoding='utf-8', errors='replace', newline='') as log:
                    frontend_proc = subprocess.Popen(
                        frontend_cmd,
                        cwd=str(root / 'frontend'),
                        stdout=log, stderr=log,
                        env=env,
                        start_new_session=not IS_WINDOWS,
                        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0,
                    )
            except OSError as exc:
                _fail(f'Failed to start frontend: {exc}. Check {frontend_log}.')
            frontend_started = {'name': 'frontend', 'pid': frontend_proc.pid, 'proc': frontend_proc, 'state': None}
            started.append(frontend_started)
            try:
                frontend_started['state'] = _register_process('frontend', frontend_proc, 'frontend', root, frontend_port)
            except RuntimeError as exc:
                _fail(f'{exc} Check {frontend_log}; if it remains active, inspect PID {frontend_proc.pid} manually.')

        # health check
        _info('Waiting for backend to be ready...')
        ready = False
        for _ in range(HEALTH_TIMEOUT):
            if _health_ok(backend_port):
                ready = True
                break
            if backend_proc.poll() is not None:
                _fail(f'Backend exited with code {backend_proc.returncode}. Check {backend_log}')
            time.sleep(1)

        if not ready:
            _fail(f'Backend did not become healthy within {HEALTH_TIMEOUT}s. Check {backend_log}')

        # save port info
        ports_info = {'backend': backend_port, 'frontend': frontend_port}
        _atomic_write_json(ports_path, ports_info)
    except BaseException:
        for item in reversed(started):
            state = item.get('state')
            if isinstance(state, dict):
                _terminate_service({**item, 'status': 'verified', 'identity': None, 'reason': ''})
                if _service_stopped(state):
                    _remove_service_artifacts(item['name'])
            else:
                # This Popen is the immediate child created in this start call;
                # use its handle only for this one cleanup, never as stored ownership.
                proc = item['proc']
                if proc.poll() is None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=5)
                    except (OSError, subprocess.TimeoutExpired):
                        _warn(f'Could not terminate unverified child PID {item["pid"]}; inspect it manually.')
        raise

    # summary
    print()
    _ok('Mission Control is running!')
    print()
    if frontend_port:
        print(f'   Web UI:   http://localhost:{frontend_port}')
    print(f'   API:      http://localhost:{backend_port}')
    print(f'   Health:   http://localhost:{backend_port}/api/health')
    print()
    print(f'   Logs:     {_run_dir() / "backend.log"}')
    if frontend_port:
        print(f'             {_run_dir() / "frontend.log"}')
    print(f'   Stop:     amc stop')
    print()

    # open browser
    if not args.no_browser and frontend_port:
        try:
            webbrowser.open(f'http://localhost:{frontend_port}')
        except Exception:
            pass

    # foreground wait with Ctrl+C
    procs = [backend_proc]
    if frontend_proc:
        procs.append(frontend_proc)

    def _shutdown(signum=None, frame=None):
        print()
        _info('Shutting down...')
        items = _inspect_services()
        backend_item = next((item for item in items if item['name'] == 'backend'), None)
        if backend_item and backend_item['status'] == 'verified':
            if _graceful_shutdown_backend(backend_item):
                time.sleep(1)
        proc_by_pid = {proc.pid: proc for proc in procs}
        stopped = 0
        uncertain = False
        for item in reversed(items):
            if item['status'] == 'stale':
                _remove_service_artifacts(item['name'])
                stopped += 1
                continue
            if item['status'] != 'verified':
                _warn(f'{_issue_text(item)}; state kept. Inspect it manually.')
                uncertain = True
                continue
            proc = proc_by_pid.get(item['pid'])
            if proc is not None:
                proc.poll()  # Reap our direct child before checking its PID again.
            same = _service_is_still_same(item['state'])
            if same is True:
                _terminate_service(item)
                if proc is not None:
                    proc.poll()
                same = _service_is_still_same(item['state'])
            if _service_stopped(item['state']):
                _remove_service_artifacts(item['name'])
                stopped += 1
            else:
                _warn(f'{item["name"]} (PID {item["pid"]}) could not be confirmed stopped; state kept.')
                uncertain = True
        if not uncertain and stopped and _ports_file_matches_services(items):
            _remove_ports_file()
        if uncertain:
            _warn('Some service state was preserved for manual inspection.')
        elif stopped:
            _ok('All services stopped.')
        else:
            _info('No verified services needed stopping.')
        sys.exit(1 if uncertain else 0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        while True:
            all_dead = _all_processes_exited(procs)
            if all_dead:
                _info('All processes have exited.')
                break
            time.sleep(1)
    except KeyboardInterrupt:
        _shutdown()


def _all_processes_exited(procs: list[subprocess.Popen]) -> bool:
    return all(code is not None for code in [proc.poll() for proc in procs])


def _graceful_shutdown_backend(item: dict | None = None) -> bool:
    """Ask only the verified AMC backend listener to shut down."""
    if item is None or item.get('status') != 'verified' or not isinstance(item.get('state'), dict):
        return False
    state = item['state']
    valid, _identity, _reason = _verify_service_state(state)
    if not valid:
        return False
    backend_port = state.get('port')
    if _port_owner_pid(backend_port) != state.get('pid'):
        _warn(f'Cannot confirm backend port {backend_port} belongs to PID {state.get("pid")}; skipping HTTP shutdown.')
        return False
    ports_file = _run_dir() / 'ports.json'
    if not ports_file.exists():
        return False
    try:
        ports = json.loads(ports_file.read_text())
        if ports.get('backend') != backend_port:
            return False
        import urllib.request
        req = urllib.request.Request(
            f'http://127.0.0.1:{backend_port}/api/admin/shutdown',
            method='POST',
            data=b'',
        )
        resp = urllib.request.urlopen(req, timeout=10)
        if resp.status == 200:
            result = json.loads(resp.read())
            aborted = result.get('aborted_tasks', [])
            if aborted:
                _info(f'Gracefully aborted {len(aborted)} active task(s)')
            return True
    except Exception:
        pass
    return False


def cmd_stop(_args: argparse.Namespace) -> None:
    run_dir = _run_dir()
    if not run_dir.exists():
        _ok('No services running.')
        return

    items = _inspect_services()
    by_name = {item['name']: item for item in items}
    backend = by_name.get('backend')
    stopped_gracefully = False
    if backend and backend['status'] == 'verified':
        _info('Requesting graceful shutdown...')
        stopped_gracefully = _graceful_shutdown_backend(backend)
        if stopped_gracefully:
            for _ in range(8):
                if _service_stopped(backend['state']):
                    break
                time.sleep(0.5)

    stopped = 0
    uncertain = False
    for item in items:
        name, pid, status = item['name'], item.get('pid'), item['status']
        if status == 'stale':
            _remove_service_artifacts(name)
            _info(f'{name} recorded process has exited; removed stale state')
            stopped += 1
            continue
        if status != 'verified':
            _warn(f'{_issue_text(item)}; state kept. Inspect it manually before removing files.')
            uncertain = True
            continue
        still_same = _service_is_still_same(item['state'])
        if _service_stopped(item['state']):
            if name == 'backend' and stopped_gracefully:
                _info(f'{name} (PID {pid}) exited after graceful shutdown')
            else:
                _info(f'{name} (PID {pid}) was already stopped')
            _remove_service_artifacts(name)
            stopped += 1
            continue
        if _terminate_service(item):
            for _ in range(8):
                still_same = _service_is_still_same(item['state'])
                if _service_stopped(item['state']):
                    break
                if still_same is None:
                    break
                time.sleep(0.25)
            if _service_stopped(item['state']):
                _info(f'Stopped {name} (PID {pid})')
                _remove_service_artifacts(name)
                stopped += 1
            else:
                _warn(f'{name} (PID {pid}) is still present; state kept for a safe retry.')
                uncertain = True
        else:
            uncertain = True

    if not uncertain and stopped and _ports_file_matches_services(items):
        _remove_ports_file()
    if uncertain:
        _warn('Some service state was preserved for manual inspection.')
        raise SystemExit(1)
    if stopped > 0:
        _ok('All services stopped.')
    else:
        _warn('No services were stopped; any unverified state was preserved for manual inspection.')


def cmd_status(_args: argparse.Namespace) -> None:
    run_dir = _run_dir()
    if not run_dir.exists():
        _info('No services registered.')
        return

    print()
    items = _inspect_services()
    by_name = {item['name']: item for item in items}
    for name in SERVICE_NAMES:
        item = by_name.get(name)
        if item is None:
            _info(f'{name}: no registered service')
        elif item['status'] == 'verified':
            _ok(f'{name} is running and identity verified (PID {item["pid"]})')
        elif item['status'] == 'stale':
            _warn(f'{name} is NOT running (PID {item["pid"]} exited)')
        else:
            _warn(f'{_issue_text(item)}; process status is unverified')

    ports = _read_json_file(run_dir / 'ports.json') or {}
    for name, service in (('backend', 'backend'), ('frontend', 'frontend')):
        port = ports.get(name)
        item = by_name.get(service)
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            continue
        if not item or item['status'] != 'verified' or item['state'].get('port') != port:
            _warn(f'{name} port {port} is not linked to a verified AMC process')
            continue
        owner = _port_owner_pid(port)
        if owner != item['pid']:
            _warn(f'{name} port {port} listener ownership could not be confirmed')
        elif name == 'backend' and _health_ok(port):
            _ok(f'Backend responding at http://127.0.0.1:{port}')
        elif name == 'frontend':
            _ok(f'Frontend listener belongs to AMC at http://127.0.0.1:{port}')
        else:
            _warn(f'Backend not responding at http://127.0.0.1:{port}')
    print()


# ── init command ──────────────────────────────────────────────────────

_BACKEND_DESC = {
    'opencode': 'OpenCode (sst/opencode)',
    'claude-code': 'Claude Code (Anthropic)',
    'copilot': 'GitHub Copilot CLI',
    'codex': 'OpenAI Codex CLI',
}


def _backend_executables() -> dict[str, str]:
    """Map backend key -> program name, straight from the adapter registry."""
    from app.adapters import AVAILABLE_BACKENDS, get_adapter
    mapping: dict[str, str] = {}
    for name in AVAILABLE_BACKENDS:
        try:
            mapping[name] = get_adapter(name).executable_name()
        except Exception:  # pragma: no cover - a broken adapter must not kill the CLI
            mapping[name] = name
    return mapping


def _detect_backends() -> list[str]:
    """Detect which agent CLIs are available on PATH."""
    import shutil
    return [name for name, cmd in _backend_executables().items() if shutil.which(cmd)]


def _prompt_choice(question: str, choices: list[str], default: str | None = None) -> str:
    """Interactive single-choice prompt."""
    print(f'\n{question}')
    for i, c in enumerate(choices, 1):
        marker = ' (default)' if c == default else ''
        print(f'  {i}. {c}{marker}')
    while True:
        hint = f' [{default}]' if default else ''
        try:
            answer = input(f'  Choose [1-{len(choices)}]{hint}: ').strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)
        if not answer and default:
            return default
        try:
            idx = int(answer)
            if 1 <= idx <= len(choices):
                return choices[idx - 1]
        except ValueError:
            if answer in choices:
                return answer
        print(f'  Please enter 1-{len(choices)}')


def _prompt_input(question: str, default: str = '') -> str:
    """Interactive text input prompt."""
    hint = f' [{default}]' if default else ''
    try:
        answer = input(f'{question}{hint}: ').strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)
    return answer or default


def cmd_init(args: argparse.Namespace) -> None:
    """Interactive setup: generates config file."""
    is_global = getattr(args, 'global_config', False)

    if is_global:
        from app.core.config import _global_config_path
        config_path = _global_config_path()
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _fail(f'Could not create config directory {config_path.parent}: {exc}')
        scope_label = 'Global'
    else:
        config_path = Path.cwd() / '.amc.yaml'
        scope_label = 'Project'

    print(f'Mission Control — {scope_label} Setup')
    print('=' * 42)

    if config_path.exists():
        try:
            overwrite = input(f'\n{_SYM_WARN} {config_path.name} already exists. Overwrite? [y/N]: ').strip().lower()
        except (EOFError, KeyboardInterrupt):
            print('\nAborted.')
            return
        if overwrite != 'y':
            print('Aborted.')
            return

    # Step 1: Detect available backends
    available = _detect_backends()
    print(f'\nDetected agent CLIs: {", ".join(available) if available else "none"}')

    if not available:
        _eprint(f'\n{_SYM_WARN} No supported agent CLI found on PATH.')
        _eprint('   Install one of: opencode, claude, gh (copilot), codex')
        _eprint('   Then re-run: amc init')
        sys.exit(1)

    # Step 2: Choose backend
    backend = _prompt_choice(
        'Which agent backend for code review?',
        available,
        default=available[0],
    )

    # Step 3: Model
    print('\nModel configuration:')
    print(f'   The model name is passed to {backend} via --model flag.')
    print('   Examples: gpt-5.4, claude-sonnet-4, glm-5.1, deepseek-r1')
    model = _prompt_input('   Model name (leave empty to use agent default)', '').strip()
    # Leave the model out when none was given, rather than writing model: ''.
    # A project file then inherits the global model instead of looking as if
    # it pinned "no model".
    backend_block = f'backend:\n  default: {backend}\n'
    if model:
        backend_block += f"  {backend}:\n    model: '{model}'\n"

    if is_global:
        # Global config: only backend + model preferences
        config_content = f"""# Mission Control global config
# Project-level .amc.yaml overrides these settings

{backend_block}"""
        try:
            config_path.write_text(config_content, encoding='utf-8')
        except OSError as exc:
            _fail(f'Could not write {config_path}: {exc}')
        print(f'\n{_SYM_OK} Created {config_path}')
        print('\nNote: this sets your default backend and model globally.')
        print('   Per-project .amc.yaml can override these.')
        print('   Run `amc init` (without --global) in a project to create project config.')
    else:
        # Project config: full settings
        default_branch = 'main'
        try:
            result = run_text(
                ['git', 'symbolic-ref', 'refs/remotes/origin/HEAD'],
                cwd=str(Path.cwd()),
            )
            if result.returncode == 0 and result.stdout:
                default_branch = result.stdout.strip().split('/')[-1]
        except Exception:
            pass
        base_branch = _prompt_input('\nBase branch for diff comparison', default_branch)

        config_content = f"""repo:
  path: .
  base_branch: {base_branch}

{backend_block}
context:
  adaptive_budget: true
  hunk_snippets_enabled: true
  include_recent_commits: 8
  candidate_files_limit: 12
  keywords_limit: 12
  top_level_entries_limit: 12
  include_top_level_entries: false
  hunk_snippet_file_limit: 3
  hunk_snippet_hunks_per_file: 1
  hunk_snippet_context_lines: 8

execution:
  idle_timeout_sec: 180

validation:
  default_mode: standard
  checks:
    build:
      command: ''
      required: false
      modes: ['standard', 'full']
    test:
      command: ''
      required: false
      modes: ['standard', 'full']
"""
        try:
            config_path.write_text(config_content, encoding='utf-8')
        except OSError as exc:
            _fail(f'Could not write {config_path}: {exc}')
        print(f'\n{_SYM_OK} Created {config_path}')

        # Show next steps
        print('\nSetup complete! Next steps:')
        print('   1. Make some code changes')
        review_cmd = 'amc review'
        if model:
            review_cmd += f' --model {model}'
        print(f'   2. Run: {review_cmd}')
        print('   3. Or configure MCP for your agent:')
        print('      {"mcpServers": {"mission-control": {"command": "amc", "args": ["mcp"]}}}')
    print()


# ── review command ─────────────────────────────────────────────────────

def cmd_review(args: argparse.Namespace) -> None:
    """Run a code review (SDK-based, no server needed)."""
    import shutil

    # Save user's working directory BEFORE chdir
    user_cwd = os.getcwd()
    output_format = args.format
    if args.rounds < 1:
        _review_error(
            f'--rounds must be a positive integer: got {args.rounds}.',
            code='invalid_rounds',
            output_format=output_format,
            hints=['Use --rounds 1 or greater.'],
        )
    if args.timeout < 1:
        _review_error(
            f'--timeout must be a positive integer of seconds: got {args.timeout}.',
            code='invalid_timeout',
            output_format=output_format,
            hints=['Use --timeout 1 or greater.'],
        )
    raw_repo = args.repo or user_cwd
    repo_path_obj = Path(raw_repo).expanduser()
    if not repo_path_obj.exists():
        _review_error(
            f'Repository path does not exist: {repo_path_obj}',
            code='repo_not_found',
            output_format=output_format,
            hints=['Pass an existing repository with --repo PATH, or run amc review from inside one.'],
        )
    if not repo_path_obj.is_dir():
        _review_error(
            f'Repository path is not a directory: {repo_path_obj}',
            code='repo_not_a_directory',
            output_format=output_format,
            hints=['--repo must point to a directory containing the repository.'],
        )
    repo_path = str(repo_path_obj.resolve())
    pr_url = args.pr_url if args.pr_url else None

    if pr_url:
        from app.core.github_ingest import is_github_pr_url
        if not is_github_pr_url(pr_url):
            _review_error(
                f'Invalid GitHub pull request URL: {pr_url}',
                code='invalid_pr_url',
                output_format=output_format,
                hints=['Expected https://github.com/<owner>/<repo>/pull/<number>.'],
            )

    backend_cmds = _backend_executables()
    supported = ', '.join(sorted(backend_cmds)) or '(none)'
    requested_backend = args.backend
    if requested_backend and requested_backend not in backend_cmds:
        _review_error(
            f'Unknown backend "{requested_backend}".',
            code='unknown_backend',
            output_format=output_format,
            hints=[f'Supported backends: {supported}.'],
        )

    from app.core.config import resolve_backend_config
    backend_name = resolve_backend_config(repo_path, backend=requested_backend)['backend']
    if backend_name not in backend_cmds:
        _review_error(
            f'Configured backend "{backend_name}" is not supported.',
            code='unknown_backend',
            output_format=output_format,
            hints=[f'Supported backends: {supported}.'],
        )

    # ── Preflight checks (local-diff mode) ────────────────────────────
    base = args.base if getattr(args, 'base', None) else None
    if not pr_url:
        try:
            git_check = run_text(['git', 'rev-parse', '--is-inside-work-tree'], cwd=repo_path)
        except FileNotFoundError:
            _review_error(
                'git is not installed or is not available on PATH.',
                code='git_unavailable',
                output_format=output_format,
                hints=['Install git and make sure `git` is available on PATH.'],
            )
        except OSError as exc:
            _review_error(
                f'Could not run git in {repo_path}: {exc}',
                code='git_unavailable',
                output_format=output_format,
                hints=['Check that the repository path is readable and git can run.'],
            )
        # A bare repository answers returncode 0 with "false"; require "true".
        if git_check.returncode != 0 or (git_check.stdout or '').strip() != 'true':
            _review_error(
                f'Not a git working tree: {repo_path}',
                code='not_a_git_repo',
                output_format=output_format,
                hints=[
                    'amc review needs a git repository to compute local changes.',
                    'Run from inside a repository, or pass --repo PATH.',
                ],
            )

        head_check = run_text(['git', 'rev-parse', '--verify', 'HEAD'], cwd=repo_path)
        if head_check.returncode != 0:
            _review_error(
                f'Repository has no commits yet: {repo_path}',
                code='no_commits',
                output_format=output_format,
                hints=['Create an initial commit before running amc review.'],
            )

        if not base:
            for candidate in ['main', 'master', 'develop']:
                check = run_text(['git', 'rev-parse', '--verify', candidate], cwd=repo_path)
                if check.returncode == 0:
                    base = candidate
                    break

        if base:
            committed = run_text(['git', 'diff', '--quiet', f'{base}...HEAD'], cwd=repo_path)
            staged = run_text(['git', 'diff', '--cached', '--quiet'], cwd=repo_path)
            unstaged = run_text(['git', 'diff', '--quiet'], cwd=repo_path)
            untracked = run_text(['git', 'ls-files', '--others', '--exclude-standard'], cwd=repo_path)
            if committed.returncode == 0 and staged.returncode == 0 and unstaged.returncode == 0 \
                    and not (untracked.stdout or '').strip():
                message = f'No changes detected against {base}.'
                _eprint(f'{_SYM_WARN} {message}')
                _eprint('    Nothing to review; make a commit, stage changes, or add an untracked file, then re-run.')
                if output_format == 'json':
                    _print_json(_no_changes_payload(base, message))
                sys.exit(0)

    # ── Backend preflight ─────────────────────────────────────────────
    required_cmd = backend_cmds[backend_name]
    if not shutil.which(required_cmd):
        detected = [name for name, cmd in backend_cmds.items() if shutil.which(cmd)]
        hints = [
            f'Install {_BACKEND_DESC.get(backend_name, backend_name)} so `{required_cmd}` is on PATH, '
            'or choose another backend with --backend.',
        ]
        if detected:
            hints.append(f'Detected backends: {", ".join(detected)}.')
        else:
            hints.append('No supported agent CLI is currently detected on PATH.')
        _review_error(
            f'Backend "{backend_name}" is not installed: `{required_cmd}` was not found on PATH.',
            code='backend_unavailable',
            output_format=output_format,
            hints=hints,
        )

    # ── Run review ────────────────────────────────────────────────────
    root = _root_dir()
    try:
        os.chdir(root)
    except OSError as exc:
        _review_error(
            f'Could not switch to the project root {root}: {exc}',
            code='chdir_failed',
            output_format=output_format,
        )

    from app.sdk import ReviewEngine

    max_rounds = args.rounds
    timeout = args.timeout
    model = args.model if getattr(args, 'model', None) else None
    reasoning = args.reasoning if getattr(args, 'reasoning', None) else None

    _eprint('Starting review...')
    if pr_url:
        _eprint(f'   PR: {pr_url}')
    else:
        _eprint(f'   Mode: local diff (base: {base or "auto-detect"})')
    _eprint(f'   Repo: {repo_path}')
    _eprint(f'   Backend: {backend_name}')
    _eprint(f'   Max rounds: {max_rounds}')

    try:
        engine = ReviewEngine(language='en', backend=backend_name, model=model, reasoning=reasoning)
        if engine.model:
            _eprint(f'   Model: {engine.model}')
        if engine.reasoning:
            _eprint(f'   Reasoning: {engine.reasoning}')
        report = engine.review(
            repo_path,
            pr_url=pr_url,
            base=base,
            review_focus=args.focus or '',
            max_rounds=max_rounds,
            timeout_sec=timeout,
            incremental=not getattr(args, 'no_incremental', False),
        )
    except KeyboardInterrupt:
        _eprint(f'\n{_SYM_FAIL} Review interrupted.')
        sys.exit(130)
    except Exception as exc:
        if os.environ.get('AMC_DEBUG'):
            import traceback
            traceback.print_exc()
        _review_error(
            f'Review could not run: {exc.__class__.__name__}: {exc}',
            code='unexpected_error',
            output_format=output_format,
            hints=['Re-run with AMC_DEBUG=1 for a traceback if the problem persists.'],
            recoverable=True,
        )

    if output_format == 'json':
        _print_json(_report_to_json(report))
    else:
        _print_review_markdown(report)

    verdict = str(getattr(report.verdict, 'value', report.verdict))
    if report.error is not None:
        _eprint(f'{_SYM_FAIL} Review failed: {report.error.get("message", "unknown error")}')
        sys.exit(2)
    if verdict == 'failed':
        _eprint(f'{_SYM_FAIL} Review failed: the backend did not produce a usable review result.')
        sys.exit(2)
    if args.exit_code and not report.passed:
        sys.exit(1)
    sys.exit(0)


def _report_to_json(report) -> dict:
    """Convert ReviewReport to JSON-serializable dict."""
    verdict = str(report.verdict.value) if hasattr(report.verdict, 'value') else str(report.verdict)
    error_message = ''
    if isinstance(report.error, dict):
        error_message = str(report.error.get('message') or '')
    output = {
        'task_id': report.task_id,
        'status': 'failed' if report.error is not None or verdict == 'failed' else 'reviewed',
        'verdict': verdict,
        'passed': report.passed,
        'finding_count': report.finding_count,
        'current_round_finding_count': len(report.findings),
        'findings': [
            {
                'severity': f.severity,
                'path': f.path,
                'line': f.line,
                'summary': f.summary,
            }
            for f in report.findings
        ],
        'summary': report.summary or error_message,
        'rounds_executed': report.rounds_executed,
        'duration_sec': report.duration_sec,
        'can_continue': report.can_continue,
    }
    if getattr(report, 'metrics', None):
        output['metrics'] = report.metrics

    # Incremental info
    if report.is_incremental:
        output['incremental'] = {
            'is_incremental': True,
            'previous_sha': report.previous_sha,
            'inferred_focus': report.inferred_focus,
            'new_findings': [
                {'severity': fs.finding.severity, 'path': fs.finding.path, 'line': fs.finding.line, 'summary': fs.finding.summary}
                for fs in report.finding_statuses if fs.status == 'new'
            ],
            'persistent_findings': [
                {'severity': fs.finding.severity, 'path': fs.finding.path, 'line': fs.finding.line, 'summary': fs.finding.summary}
                for fs in report.finding_statuses if fs.status == 'persistent'
            ],
            'resolved_findings': [
                {'severity': f.severity, 'path': f.path, 'line': f.line, 'summary': f.summary}
                for f in report.resolved_findings
            ],
            'unverified_findings': [
                {'severity': f.severity, 'path': f.path, 'line': f.line, 'summary': f.summary}
                for f in report.unverified_findings
            ],
        }

    if report.error is not None:
        output['error'] = report.error
    elif verdict == 'failed':
        output['error'] = {
            'code': 'review_failed',
            'message': report.summary or 'The backend did not produce a usable review result.',
            'recoverable': True,
        }

    return output


def _print_review_markdown(report) -> None:
    """Pretty-print review report to terminal."""
    marker = _SYM_OK if report.passed else _SYM_FAIL
    print(f'{marker} Verdict: {report.verdict}')
    print(f'   Rounds: {report.rounds_executed} | Duration: {report.duration_sec:.1f}s')

    if report.is_incremental:
        print(f'   Mode: incremental (since {report.previous_sha[:7]})')
    if report.inferred_focus:
        print(f'   Auto-focus: {report.inferred_focus}')
    print()

    if report.error:
        print(f'{_SYM_WARN} Error: {report.error["message"]}')
        print()

    if report.summary:
        print(f'Summary: {report.summary}')
        print()

    # Show findings with status if incremental
    if report.findings or (report.is_incremental and report.finding_statuses):
        if report.is_incremental and report.finding_statuses:
            new_count = sum(1 for fs in report.finding_statuses if fs.status == 'new')
            persistent_count = sum(1 for fs in report.finding_statuses if fs.status == 'persistent')
            unverified_count = sum(1 for fs in report.finding_statuses if fs.status == 'unverified')
            print(f'Findings ({report.finding_count}): {new_count} new, {persistent_count} persistent, {unverified_count} unverified')
            for fs in report.finding_statuses:
                f = fs.finding
                loc = ''
                if f.path:
                    loc = f' ({f.path}'
                    if f.line:
                        loc += f':{f.line}'
                    loc += ')'
                print(f'   - [{fs.status}] [{f.severity}]{loc} {f.summary}')
        else:
            print(f'Findings ({report.finding_count}):')
            for f in report.findings:
                loc = ''
                if f.path:
                    loc = f' ({f.path}'
                    if f.line:
                        loc += f':{f.line}'
                    loc += ')'
                print(f'   - [{f.severity}]{loc} {f.summary}')
        print()

    # Show resolved findings
    if report.resolved_findings:
        print(f'Resolved ({len(report.resolved_findings)}):')
        for f in report.resolved_findings:
            loc = f' ({f.path}:{f.line})' if f.path else ''
            print(f'   - [fixed] [{f.severity}]{loc} {f.summary}')
        print()

    if report.can_continue:
        print('Note: more rounds are available to address findings.')


# ── mcp command ───────────────────────────────────────────────────────

def cmd_mcp(args: argparse.Namespace) -> None:
    """Start MCP server (stdio mode)."""
    root = _root_dir()
    os.chdir(root)

    # Show model info on stderr so user knows what's being used
    import sys
    from app.adapters.direct_api_adapter import resolve_direct_api_config
    from app.core.config import load_repo_config
    cfg = load_repo_config('.')
    api_cfg = resolve_direct_api_config(cfg)
    if api_cfg.is_valid():
        print(f"[amc mcp] Direct API: model={api_cfg.model}, wire={api_cfg.wire_api}", file=sys.stderr)
        amc_model = cfg.get('backend', {}).get(cfg.get('backend', {}).get('default', ''), {}).get('model', '')
        if amc_model and amc_model != api_cfg.model:
            print(f"[amc mcp] Note: amc config has model={amc_model} (used by CLI mode).", file=sys.stderr)
            print(f"[amc mcp]       MCP uses {api_cfg.model} from Codex config (direct API doesn't support {amc_model}).", file=sys.stderr)
    else:
        print("[amc mcp] Warning: Direct API config incomplete. Review will fail.", file=sys.stderr)
        print("[amc mcp] Configure ~/.codex/config.toml or backend.direct_api in amc config.", file=sys.stderr)

    from app.mcp_server import serve
    serve()


# ── main ──────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog='amc',
        description='Mission Control — PR Review Console',
    )
    sub = parser.add_subparsers(dest='command', required=True)

    start_p = sub.add_parser('start', help='Start all services')
    start_p.add_argument('--port', type=int, default=BACKEND_DEFAULT_PORT, help='Backend port (default: 8000)')
    start_p.add_argument('--ui-port', type=int, default=FRONTEND_DEFAULT_PORT, help='Frontend port (default: 5173)')
    start_p.add_argument('--no-ui', action='store_true', help='Skip frontend')
    start_p.add_argument('--no-browser', action='store_true', help='Do not open browser')

    sub.add_parser('stop', help='Stop all services')
    sub.add_parser('status', help='Show service status')

    # init subcommand
    init_p = sub.add_parser('init', help='Interactive setup (generates .amc.yaml)')
    init_p.add_argument('--global', dest='global_config', action='store_true',
                        help='Write to the user-wide config (~/.config/amc/config.yaml or %%APPDATA%%\\amc\\config.yaml on Windows)')

    # review subcommand
    review_p = sub.add_parser('review', help='Run code review (no server needed)')
    review_p.add_argument('pr_url', nargs='?', default=None, help='GitHub PR URL (optional; omit for local diff)')
    review_p.add_argument('--repo', '-r', default=None, help='Repository path (default: cwd)')
    review_p.add_argument('--base', '-b', default=None, help='Base branch for local diff (default: auto-detect)')
    review_p.add_argument('--backend', default=None, help='Backend agent (opencode, claude-code, copilot, codex)')
    review_p.add_argument('--model', '-m', default=None, help='Model override (priority: --model > $AMC_MODEL > .amc.yaml)')
    review_p.add_argument('--reasoning', default=None, help='Reasoning effort override (priority: --reasoning > $AMC_REASONING > .amc.yaml)')
    review_p.add_argument('--rounds', type=int, default=1,
                          help='Positive integer, max review rounds (default: 1)')
    review_p.add_argument('--format', choices=['markdown', 'json'], default='markdown',
                          help='Output format (json: stdout is JSON only; progress goes to stderr)')
    review_p.add_argument('--focus', '-f', default=None, help='Review focus (e.g. "security", "performance")')
    review_p.add_argument('--timeout', type=int, default=600,
                          help='Positive integer, per-round timeout in seconds (default: 600)')
    review_p.add_argument('--exit-code', action='store_true',
                          help='Exit 1 if review has concerns; setup/runtime errors exit 2')
    review_p.add_argument('--no-incremental', action='store_true', help='Review all current changes and replace prior findings')

    # mcp subcommand
    sub.add_parser('mcp', help='Start MCP tool server (stdio)')

    args = parser.parse_args()

    commands = {
        'start': cmd_start,
        'stop': cmd_stop,
        'status': cmd_status,
        'init': cmd_init,
        'review': cmd_review,
        'mcp': cmd_mcp,
    }
    commands[args.command](args)


if __name__ == '__main__':
    main()
