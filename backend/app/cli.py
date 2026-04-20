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

RUN_DIR_NAME = '.run'
BACKEND_DEFAULT_PORT = 8000
FRONTEND_DEFAULT_PORT = 5173
HEALTH_TIMEOUT = 30

IS_WINDOWS = sys.platform == 'win32'


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
    print(f'\033[0;36m{msg}\033[0m')


def _ok(msg: str) -> None:
    print(f'\033[0;32m✅ {msg}\033[0m')


def _warn(msg: str) -> None:
    print(f'\033[0;33m⚠️  {msg}\033[0m')


def _fail(msg: str) -> None:
    print(f'\033[0;31m❌ {msg}\033[0m')
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
            result = subprocess.run(
                ['tasklist', '/FI', f'PID eq {pid}', '/NH'],
                capture_output=True, text=True,
            )
            return str(pid) in result.stdout
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
    if not _which('node'):
        return
    _info('Installing frontend dependencies...')
    subprocess.run(
        ['npm', 'install', '--silent'],
        cwd=str(root / 'frontend'),
        check=True,
        shell=IS_WINDOWS,
    )
    _ok('Frontend dependencies ready')


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
        node_ver = subprocess.run(['node', '--version'], capture_output=True, text=True).stdout.strip()
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
        '--reload',
    ]
    with open(backend_log, 'w') as log:
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
        npm_cmd = 'npm.cmd' if IS_WINDOWS else 'npm'
        frontend_cmd = [
            npm_cmd, 'run', 'dev', '--',
            '--host', '127.0.0.1',
            '--port', str(frontend_port),
        ]
        with open(frontend_log, 'w') as log:
            frontend_proc = subprocess.Popen(
                frontend_cmd,
                cwd=str(root / 'frontend'),
                stdout=log, stderr=log,
                env=env,
                start_new_session=not IS_WINDOWS,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0,
                shell=IS_WINDOWS,
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

_BACKEND_INFO = {
    'opencode': {'cmd': 'opencode', 'desc': 'OpenCode (sst/opencode)'},
    'claude-code': {'cmd': 'claude', 'desc': 'Claude Code (Anthropic)'},
    'copilot': {'cmd': 'gh', 'desc': 'GitHub Copilot CLI'},
    'codex': {'cmd': 'codex', 'desc': 'OpenAI Codex CLI'},
}


def _detect_backends() -> list[str]:
    """Detect which agent CLIs are available on PATH."""
    import shutil
    available = []
    for name, info in _BACKEND_INFO.items():
        if shutil.which(info['cmd']):
            available.append(name)
    return available


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
    """Interactive setup: generates .amc.yaml in the current directory."""
    config_path = Path.cwd() / '.amc.yaml'

    print('🚀 Mission Control — Interactive Setup')
    print('=' * 42)

    if config_path.exists():
        try:
            overwrite = input(f'\n⚠️  .amc.yaml already exists. Overwrite? [y/N]: ').strip().lower()
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

    # Step 4: Base branch
    default_branch = 'main'
    try:
        result = subprocess.run(
            ['git', 'symbolic-ref', 'refs/remotes/origin/HEAD'],
            capture_output=True, text=True, cwd=str(Path.cwd()),
        )
        if result.returncode == 0:
            default_branch = result.stdout.strip().split('/')[-1]
    except Exception:
        pass
    base_branch = _prompt_input(f'\n🌿 Base branch for diff comparison', default_branch)

    # Step 5: Generate .amc.yaml
    config_content = f"""repo:
  path: .
  base_branch: {base_branch}

backend:
  default: {backend}
  opencode:
    model: '{model if backend == "opencode" else ""}'
    variant: ''

context:
  include_recent_commits: 8
  candidate_files_limit: 12

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
    config_path.write_text(config_content)
    print(f'\n✅ Created {config_path}')

    # Step 6: Show next steps
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
    # Ensure we're in project dir for relative imports
    root = _root_dir()
    os.chdir(root)

    # Import SDK (heavy imports deferred)
    from app.sdk import ReviewEngine, ReviewReport

    repo_path = args.repo or os.getcwd()
    pr_url = args.pr_url if args.pr_url else None
    base = args.base if hasattr(args, 'base') and args.base else None
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
    print(f'   Max rounds: {max_rounds}')

    engine = ReviewEngine(language='en', backend=args.backend, model=model)
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
    sub.add_parser('init', help='Interactive setup (generates .amc.yaml)')

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
