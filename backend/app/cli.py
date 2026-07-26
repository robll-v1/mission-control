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

if IS_WINDOWS:
    _SYM_OK = '[OK]'
    _SYM_WARN = '[!]'
    _SYM_FAIL = '[x]'
else:
    _SYM_OK = '✅'
    _SYM_WARN = '⚠️ '
    _SYM_FAIL = '❌'


def _color(code: str, msg: str) -> str:
    if not _USE_COLOR:
        return msg
    return f'\033[{code}m{msg}\033[0m'


def _root_dir() -> Path:
    """Resolve project root (directory containing pyproject.toml)."""
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
    print(_color('0;36', msg))


def _ok(msg: str) -> None:
    print(_color('0;32', f'{_SYM_OK} {msg}'))


def _warn(msg: str) -> None:
    print(_color('0;33', f'{_SYM_WARN} {msg}'))


def _fail(msg: str) -> None:
    print(_color('0;31', f'{_SYM_FAIL} {msg}'))
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
        except ValueError:
            return None
    return None


def _pid_alive(pid: int) -> bool:
    try:
        if IS_WINDOWS:
            result = run_text(['tasklist', '/FI', f'PID eq {pid}', '/NH'])
            return str(pid) in (result.stdout or '')
        else:
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError):
        return False


def _kill_pid(pid: int) -> bool:
    try:
        if IS_WINDOWS:
            subprocess.run(
                ['taskkill', '/F', '/T', '/PID', str(pid)],
                capture_output=True,
            )
        else:
            os.kill(pid, signal.SIGTERM)
            for _ in range(10):
                time.sleep(0.5)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return True
            os.kill(pid, signal.SIGKILL)
        return True
    except (OSError, ProcessLookupError):
        return True


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
        _ok('Using existing virtual environment')
        return

    _info('First run — setting up virtual environment...')
    python = sys.executable
    subprocess.run([python, '-m', 'venv', str(venv_dir)], check=True)
    vpy = str(_venv_python())
    subprocess.run([vpy, '-m', 'pip', 'install', '--quiet', '--upgrade', 'pip'], check=True)
    subprocess.run([vpy, '-m', 'pip', 'install', '--quiet', '-e', str(root)], check=True)
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
    subprocess.run(
        [npm_bin, 'install', '--silent'],
        cwd=str(root / 'frontend'),
        check=True,
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

    # prerequisites
    _info('Checking prerequisites...')

    python_ver = f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}'
    if sys.version_info < (3, 11):
        _fail(f'Python 3.11+ required, found {python_ver}')
    _ok(f'Python: {python_ver}')

    has_node = bool(_which('node'))
    if has_node:
        node_ver = (run_text(['node', '--version']).stdout or '').strip()
        _ok(f'Node.js: {node_ver}')
    else:
        _warn('Node.js not found. Frontend will not be started.')

    if _which('opencode'):
        _ok('OpenCode: found')
    else:
        _warn('OpenCode not found in PATH. Reviews will fail until it is installed.')

    gh_token = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
    if not gh_token:
        _warn('No GITHUB_TOKEN or GH_TOKEN set. GitHub API rate limits will apply.')

    # bootstrap
    _ensure_venv()
    skip_ui = args.no_ui or not has_node
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
    with open(backend_log, 'w', encoding='utf-8', errors='replace', newline='') as log:
        backend_proc = subprocess.Popen(
            backend_cmd,
            stdout=log, stderr=log,
            start_new_session=not IS_WINDOWS,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0,
        )
    _save_pid('backend', backend_proc.pid)

    # start frontend
    frontend_proc = None
    if not skip_ui and frontend_port is not None:
        _info(f'Starting frontend on port {frontend_port}...')
        frontend_log = _run_dir() / 'frontend.log'
        env = os.environ.copy()
        env['VITE_API_TARGET'] = f'http://127.0.0.1:{backend_port}'
        npm_bin = _resolve_npm()
        if not npm_bin:
            _fail('npm not found in PATH')
        frontend_cmd = [
            npm_bin, 'run', 'dev', '--',
            '--host', '127.0.0.1',
            '--port', str(frontend_port),
        ]
        with open(frontend_log, 'w', encoding='utf-8', errors='replace', newline='') as log:
            frontend_proc = subprocess.Popen(
                frontend_cmd,
                cwd=str(root / 'frontend'),
                stdout=log, stderr=log,
                env=env,
                start_new_session=not IS_WINDOWS,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0,
            )
        _save_pid('frontend', frontend_proc.pid)

    # health check
    _info('Waiting for backend to be ready...')
    ready = False
    for _ in range(HEALTH_TIMEOUT):
        if _health_ok(backend_port):
            ready = True
            break
        if not _pid_alive(backend_proc.pid):
            _fail(f'Backend exited unexpectedly. Check {backend_log}')
        time.sleep(1)

    if not ready:
        _fail(f'Backend did not become healthy within {HEALTH_TIMEOUT}s. Check {backend_log}')

    # save port info
    ports_info = {'backend': backend_port, 'frontend': frontend_port}
    (_run_dir() / 'ports.json').write_text(json.dumps(ports_info))

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
    pids = [backend_proc.pid]
    if frontend_proc:
        pids.append(frontend_proc.pid)

    def _shutdown(signum=None, frame=None):
        print()
        _info('Shutting down...')
        _graceful_shutdown_backend()
        time.sleep(1)
        for pid in pids:
            _kill_pid(pid)
        import shutil
        shutil.rmtree(str(_run_dir()), ignore_errors=True)
        _ok('All services stopped.')
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        while True:
            all_dead = all(not _pid_alive(p) for p in pids)
            if all_dead:
                _info('All processes have exited.')
                break
            time.sleep(1)
    except KeyboardInterrupt:
        _shutdown()


def _graceful_shutdown_backend() -> bool:
    """Ask backend to gracefully abort active tasks and shut down. Returns True on success."""
    ports_file = _run_dir() / 'ports.json'
    if not ports_file.exists():
        return False
    try:
        ports = json.loads(ports_file.read_text())
        backend_port = ports.get('backend')
        if not backend_port:
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

    # Try graceful shutdown first (aborts active reviews)
    backend_pid = _read_pid('backend')
    if backend_pid and _pid_alive(backend_pid):
        _info('Requesting graceful shutdown...')
        if _graceful_shutdown_backend():
            # Wait a bit for backend to finish aborting tasks
            for _ in range(8):
                if not _pid_alive(backend_pid):
                    break
                time.sleep(0.5)

    stopped = 0
    for pidfile in sorted(run_dir.glob('*.pid')):
        name = pidfile.stem
        pid = _read_pid(name)
        if pid is None:
            continue
        if _pid_alive(pid):
            _kill_pid(pid)
            _info(f'Stopped {name} (PID {pid})')
            stopped += 1
        else:
            _info(f'{name} (PID {pid}) was already stopped')

    import shutil
    shutil.rmtree(str(run_dir), ignore_errors=True)

    if stopped > 0:
        _ok('All services stopped.')
    else:
        _ok('No running services found.')


def cmd_status(_args: argparse.Namespace) -> None:
    run_dir = _run_dir()
    if not run_dir.exists():
        _info('No services registered.')
        return

    print()
    for pidfile in sorted(run_dir.glob('*.pid')):
        name = pidfile.stem
        pid = _read_pid(name)
        if pid is None:
            continue
        if _pid_alive(pid):
            _ok(f'{name} is running (PID {pid})')
        else:
            _warn(f'{name} is NOT running (PID {pid} exited)')

    # check endpoints
    ports_file = run_dir / 'ports.json'
    if ports_file.exists():
        try:
            ports = json.loads(ports_file.read_text())
        except Exception:
            ports = {}
        print()
        backend_port = ports.get('backend')
        if backend_port and _health_ok(backend_port):
            _ok(f'Backend responding at http://127.0.0.1:{backend_port}')
        elif backend_port:
            _warn(f'Backend not responding at http://127.0.0.1:{backend_port}')

        frontend_port = ports.get('frontend')
        if frontend_port:
            if _port_is_free(frontend_port):
                _warn(f'Frontend not responding at http://127.0.0.1:{frontend_port}')
            else:
                _ok(f'Frontend responding at http://127.0.0.1:{frontend_port}')
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
        config_path.parent.mkdir(parents=True, exist_ok=True)
        scope_label = 'Global'
    else:
        config_path = Path.cwd() / '.amc.yaml'
        scope_label = 'Project'

    print(f'🚀 Mission Control — {scope_label} Setup')
    print('=' * 42)

    if config_path.exists():
        try:
            overwrite = input(f'\n⚠️  {config_path.name} already exists. Overwrite? [y/N]: ').strip().lower()
        except (EOFError, KeyboardInterrupt):
            print('\nAborted.')
            return
        if overwrite != 'y':
            print('Aborted.')
            return

    # Step 1: Detect available backends
    available = _detect_backends()
    print(f'\n📡 Detected agent CLIs: {", ".join(available) if available else "none"}')

    if not available:
        print('\n⚠️  No supported agent CLI found on PATH.')
        print('   Install one of: opencode, claude, gh (copilot), codex')
        print('   Then re-run: amc init')
        return

    # Step 2: Choose backend
    backend = _prompt_choice(
        '🤖 Which agent backend for code review?',
        available,
        default=available[0],
    )

    # Step 3: Model
    print(f'\n📌 Model configuration:')
    print(f'   The model name is passed to {backend} via --model flag.')
    print(f'   Examples: gpt-5.4, claude-sonnet-4, glm-5.1, deepseek-r1')
    model = _prompt_input('   Model name (leave empty to use agent default)', '')

    if is_global:
        # Global config: only backend + model preferences
        config_content = f"""# Mission Control global config
# Project-level .amc.yaml overrides these settings

backend:
  default: {backend}
  {backend}:
    model: '{model}'
"""
        config_path.write_text(config_content, encoding='utf-8')
        print(f'\n✅ Created {config_path}')
        print(f'\n💡 This sets your default backend and model globally.')
        print(f'   Per-project .amc.yaml can override these.')
        print(f'   Run `amc init` (without --global) in a project to create project config.')
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
        base_branch = _prompt_input(f'\n🌿 Base branch for diff comparison', default_branch)

        config_content = f"""repo:
  path: .
  base_branch: {base_branch}

backend:
  default: {backend}
  {backend}:
    model: '{model}'

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
        config_path.write_text(config_content, encoding='utf-8')
        print(f'\n✅ Created {config_path}')

        # Show next steps
        print(f'\n🎉 Setup complete! Next steps:')
        print(f'   1. Make some code changes')
        review_cmd = 'amc review'
        if model:
            review_cmd += f' --model {model}'
        print(f'   2. Run: {review_cmd}')
        print(f'   3. Or configure MCP for your agent:')
        print(f'      {{"mcpServers": {{"mission-control": {{"command": "amc", "args": ["mcp"]}}}}}}')
    print()


# ── review command ─────────────────────────────────────────────────────

def cmd_review(args: argparse.Namespace) -> None:
    """Run a code review (SDK-based, no server needed)."""
    import shutil

    # Save user's working directory BEFORE chdir
    user_cwd = os.getcwd()
    repo_path = args.repo or user_cwd
    pr_url = args.pr_url if args.pr_url else None
    backend_name = args.backend

    # ── Preflight checks ──────────────────────────────────────────────
    # Check 1: Is this a git repo?
    if not pr_url:
        git_check = run_text(['git', 'rev-parse', '--is-inside-work-tree'], cwd=repo_path)
        if git_check.returncode != 0:
            print(f'❌ Not a git repository: {repo_path}')
            print(f'   amc review needs a git repo to compute diffs.')
            print(f'   Run from inside a git repo, or use --repo PATH.')
            sys.exit(1)

    # Check 2: Is the backend CLI available?
    # Source the backend -> executable mapping from the adapter registry so this
    # preflight and ExecutionService._preflight_check cannot drift apart.
    backend_cmds = _backend_executables()
    required_cmd = backend_cmds.get(backend_name, backend_name)
    if not shutil.which(required_cmd):
        print(f'❌ Backend "{backend_name}" not found: `{required_cmd}` is not on PATH.')
        print(f'   Install it or choose a different backend with --backend.')
        available = [name for name, cmd in backend_cmds.items() if shutil.which(cmd)]
        if available:
            print(f'   Available backends: {", ".join(available)}')
        sys.exit(1)

    # Check 3: Any changes to review? (local-diff mode only)
    if not pr_url:
        base = args.base if hasattr(args, 'base') and args.base else None
        # Try to detect base branch
        if not base:
            for candidate in ['main', 'master', 'develop']:
                check = run_text(['git', 'rev-parse', '--verify', candidate], cwd=repo_path)
                if check.returncode == 0:
                    base = candidate
                    break
        if base:
            diff_stat = run_text(['git', 'diff', '--stat', f'{base}..HEAD'], cwd=repo_path)
            staged = run_text(['git', 'diff', '--cached', '--stat'], cwd=repo_path)
            unstaged = run_text(['git', 'diff', '--stat'], cwd=repo_path)
            if not (diff_stat.stdout or '').strip() and not (staged.stdout or '').strip() \
                    and not (unstaged.stdout or '').strip():
                print(f'⚠️  No changes detected (base: {base})')
                print(f'   Nothing to review — your branch is identical to {base}.')
                print(f'   Make some commits or stage changes, then try again.')
                sys.exit(0)
    else:
        base = args.base if hasattr(args, 'base') and args.base else None

    # ── End preflight ─────────────────────────────────────────────────

    # Ensure we're in project dir for relative imports
    root = _root_dir()
    os.chdir(root)

    # Import SDK (heavy imports deferred)
    from app.sdk import ReviewEngine, ReviewReport

    max_rounds = args.rounds
    timeout = args.timeout
    output_format = args.format
    model = args.model if hasattr(args, 'model') and args.model else None

    print(f'🔍 Starting review...')
    if pr_url:
        print(f'   PR: {pr_url}')
    else:
        print(f'   Mode: local diff (base: {base or "auto-detect"})')
    print(f'   Repo: {repo_path}')
    print(f'   Backend: {backend_name}')
    print(f'   Max rounds: {max_rounds}')

    engine = ReviewEngine(language='en', backend=backend_name, model=model)
    if engine.model:
        print(f'   Model: {engine.model}')
    print()

    report = engine.review(
        repo_path,
        pr_url=pr_url,
        base=base,
        review_focus=args.focus or '',
        max_rounds=max_rounds,
        timeout_sec=timeout,
    )

    # Output result
    if output_format == 'json':
        output = _report_to_json(report)
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        _print_review_markdown(report)

    # Exit code
    if args.exit_code:
        sys.exit(0 if report.passed else 1)


def _report_to_json(report) -> dict:
    """Convert ReviewReport to JSON-serializable dict."""
    output = {
        'task_id': report.task_id,
        'verdict': str(report.verdict.value) if hasattr(report.verdict, 'value') else str(report.verdict),
        'passed': report.passed,
        'finding_count': report.finding_count,
        'findings': [
            {
                'severity': f.severity,
                'path': f.path,
                'line': f.line,
                'summary': f.summary,
            }
            for f in report.findings
        ],
        'summary': report.summary,
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
        }

    if report.error:
        output['error'] = report.error

    return output


def _print_review_markdown(report) -> None:
    """Pretty-print review report to terminal."""
    icon = '✅' if report.passed else '❌'
    print(f'{icon} Verdict: {report.verdict}')
    print(f'   Rounds: {report.rounds_executed} | Duration: {report.duration_sec:.1f}s')

    if report.is_incremental:
        print(f'   Mode: incremental (since {report.previous_sha[:7]})')
    if report.inferred_focus:
        print(f'   Auto-focus: {report.inferred_focus}')
    print()

    if report.error:
        print(f'⚠️  Error: {report.error["message"]}')
        print()

    if report.summary:
        print(f'📋 Summary: {report.summary}')
        print()

    # Show findings with status if incremental
    if report.findings:
        if report.is_incremental and report.finding_statuses:
            new_count = sum(1 for fs in report.finding_statuses if fs.status == 'new')
            persistent_count = sum(1 for fs in report.finding_statuses if fs.status == 'persistent')
            print(f'🔎 Findings ({report.finding_count}): {new_count} new, {persistent_count} persistent')
            for fs in report.finding_statuses:
                f = fs.finding
                status_icon = '🆕' if fs.status == 'new' else '🔄'
                loc = ''
                if f.path:
                    loc = f' ({f.path}'
                    if f.line:
                        loc += f':{f.line}'
                    loc += ')'
                print(f'   {status_icon} [{f.severity}]{loc} {f.summary}')
        else:
            print(f'🔎 Findings ({report.finding_count}):')
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
        print(f'✅ Resolved ({len(report.resolved_findings)}):')
        for f in report.resolved_findings:
            loc = f' ({f.path}:{f.line})' if f.path else ''
            print(f'   ✓ [{f.severity}]{loc} {f.summary}')
        print()

    if report.can_continue:
        print('💡 Can continue with more rounds to address findings.')


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
    review_p.add_argument('--backend', default='opencode', help='Backend agent (opencode, claude-code, copilot, codex)')
    review_p.add_argument('--model', '-m', default=None, help='Model override (priority: --model > $AMC_MODEL > .amc.yaml)')
    review_p.add_argument('--rounds', type=int, default=1, help='Max review rounds (default: 1)')
    review_p.add_argument('--format', choices=['markdown', 'json'], default='markdown', help='Output format')
    review_p.add_argument('--focus', '-f', default=None, help='Review focus (e.g. "security", "performance")')
    review_p.add_argument('--timeout', type=int, default=600, help='Per-round timeout in seconds (default: 600)')
    review_p.add_argument('--exit-code', action='store_true', help='Exit with code 1 if review has concerns')

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
