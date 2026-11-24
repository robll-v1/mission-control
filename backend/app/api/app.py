import asyncio
import logging
import os
import signal
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.adapters import get_adapter, AVAILABLE_BACKENDS
from app.api.routes_artifacts import router as artifact_router
from app.api.routes_control import router as control_router
from app.api.routes_config import router as config_router
from app.api.routes_stream import router as stream_router
from app.api.routes_tasks import router as task_router
from app.api.security import LocalSecurityMiddleware
from app.core.config import load_repo_config, resolve_backend_config
from app.core.context_compiler import ContextCompiler
from app.core.db import Database
from app.core.execution import ExecutionService
from app.core.mission_engine import MissionEngine
from app.core.models import TaskStatus
from app.core.paths import (
    DataDirLock,
    get_bundled_ui_dir,
    get_data_dir,
    get_db_path,
    get_runtime_dir,
    get_worktrees_dir,
    maybe_import_legacy_db,
)
from app.core.recovery import recover_stale_tasks
from app.core.validation import ValidationService
from app.core.worktree import WorktreeManager
from app.services.artifact_store import ArtifactStore
from app.services.diff_service import DiffService
from app.services.summary_service import SummaryService

logger = logging.getLogger(__name__)


DATA_DIR = get_data_dir()
DB_PATH = get_db_path()
RUNTIME_ROOT = str(get_runtime_dir())
WORKTREES_ROOT = str(get_worktrees_dir())

_checkout_root = Path(__file__).resolve().parent.parent.parent.parent
_extra_legacy_roots = [_checkout_root] if (_checkout_root / 'pyproject.toml').is_file() else []
_imported_legacy_db = maybe_import_legacy_db(extra_roots=_extra_legacy_roots)
if _imported_legacy_db is not None:
    logger.info('Imported legacy database from %s into %s', _imported_legacy_db, DB_PATH)

db = Database(str(DB_PATH))
engine = MissionEngine(db)
adapters = {name: get_adapter(name) for name in AVAILABLE_BACKENDS}
worktrees = WorktreeManager(RUNTIME_ROOT, worktrees_root=WORKTREES_ROOT)
artifacts = ArtifactStore(RUNTIME_ROOT)
contexts = ContextCompiler(artifacts)
execution = ExecutionService(
    db=db, mission=engine, adapters=adapters, worktrees=worktrees, artifacts=artifacts, contexts=contexts,
    adapter_factory=lambda name, *, model=None, variant=None, reasoning=None: get_adapter(
        name, model=model, variant=variant, reasoning=reasoning,
    ),
)
validation = ValidationService(db=db, mission=engine, artifacts=artifacts)
diffs = DiffService(artifacts)
summaries = SummaryService(db, artifacts)


def _prune_startup_worktrees() -> None:
    tasks = db.list_tasks()
    repo_task_ids: dict[str, set[str]] = {}
    active_like_statuses = {TaskStatus.INGESTING, TaskStatus.RUNNING, TaskStatus.VALIDATING}
    for task in tasks:
        cfg = load_repo_config(task.repo_path)
        worktree_cfg = cfg.get('worktree', {}) if isinstance(cfg, dict) else {}
        if not worktree_cfg.get('prune_on_start', True):
            continue
        repo_task_ids.setdefault(task.repo_path, set()).add(task.id)
        if task.status in active_like_statuses:
            continue
        try:
            result = worktrees.cleanup_worktree(task)
        except Exception:
            continue
        changed = False
        if not result.get('exists_after') and task.worktree_path is not None:
            task.worktree_path = None
            changed = True
        if result.get('branch_removed') and (task.branch_name or '') == f'review/{task.id}':
            task.branch_name = None
            changed = True
        if changed:
            db.save_task(task)

    for repo_path, task_ids in repo_task_ids.items():
        try:
            worktrees.prune_orphaned_worktrees(repo_path, known_task_ids=task_ids)
        except Exception:
            continue


_data_dir_lock = DataDirLock(DATA_DIR)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup exclusivity/recovery and graceful shutdown for SSE streams and active tasks."""
    if not _data_dir_lock.acquire():
        holder = _data_dir_lock.holder_pid()
        holder_hint = f' (PID {holder})' if holder else ''
        raise RuntimeError(
            f'Another Mission Control backend is already using data directory {DATA_DIR}{holder_hint}. '
            'Stop it first or set a separate AMC_DATA_DIR.'
        )

    # Recovery runs after acquiring the data-dir lock and before pruning:
    # worktree pruning treats VALIDATING (and other mid-flight statuses) as
    # active and skips them, so stale states must be cleared first.
    recover_stale_tasks(db, engine)
    _prune_startup_worktrees()

    prev_sig_handlers: dict[int, object] = {}
    prev_loop_handlers: dict[int, tuple] = {}
    if threading.current_thread() is threading.main_thread():
        # Uvicorn waits for active StreamingResponses to end BEFORE invoking
        # lifespan teardown. Hook Uvicorn's installed signal handler (via
        # asyncio loop signal handlers on POSIX, or callable signal.signal
        # handlers on Windows) so open SSE streams close immediately while
        # Uvicorn still runs full graceful teardown (exit code 0).
        loop = asyncio.get_running_loop()
        loop_handlers = getattr(loop, '_signal_handlers', None)
        sigs = [signal.SIGINT]
        if hasattr(signal, 'SIGTERM'):
            sigs.append(signal.SIGTERM)
        for sig in sigs:
            if isinstance(loop_handlers, dict) and sig in loop_handlers:
                handle = loop_handlers[sig]
                orig_cb = getattr(handle, '_callback', None)
                orig_args = getattr(handle, '_args', ())
                if callable(orig_cb):
                    prev_loop_handlers[sig] = (orig_cb, orig_args)

                    def _make_loop_cb(cb, args):
                        def _cb():
                            engine.event_bus.close_all()
                            cb(*args)
                        return _cb

                    try:
                        loop.add_signal_handler(sig, _make_loop_cb(orig_cb, orig_args))
                        continue
                    except (NotImplementedError, RuntimeError, ValueError):
                        prev_loop_handlers.pop(sig, None)
            try:
                prev = signal.getsignal(sig)
                if callable(prev):
                    prev_sig_handlers[sig] = prev

                    def _make_sig_handler(previous, target_loop):
                        def _handler(signum, frame):
                            # Never acquire EventBus._lock synchronously inside a
                            # signal handler (could self-deadlock if the signal
                            # interrupted subscribe()/unsubscribe() on this thread).
                            # Schedule close_all() onto the event loop instead.
                            try:
                                target_loop.call_soon_threadsafe(engine.event_bus.close_all)
                            except RuntimeError:
                                pass
                            previous(signum, frame)
                        return _handler

                    signal.signal(sig, _make_sig_handler(prev, loop))
            except (OSError, ValueError):
                pass
    try:
        yield
    finally:
        # ── shutdown ──
        engine.event_bus.close_all()
        active_ids = list(execution._active.keys())
        if active_ids:
            logger.info('Graceful shutdown: aborting %d active task(s)...', len(active_ids))
            for task_id in active_ids:
                try:
                    execution.abort_task(task_id)
                    logger.info('Aborted task %s', task_id)
                except Exception:
                    logger.exception('Failed to abort task %s during shutdown', task_id)
        if prev_loop_handlers:
            try:
                loop = asyncio.get_running_loop()
                for sig, (orig_cb, orig_args) in prev_loop_handlers.items():
                    loop.add_signal_handler(sig, orig_cb, *orig_args)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        for sig, prev in prev_sig_handlers.items():
            try:
                signal.signal(sig, prev)
            except (OSError, ValueError):
                pass
        _data_dir_lock.release()
        logger.info('Shutdown complete.')


app = FastAPI(title='PR Review Control', lifespan=lifespan)
app.add_middleware(LocalSecurityMiddleware)
app.include_router(task_router)
app.include_router(config_router)
app.include_router(stream_router)
app.include_router(control_router)
app.include_router(artifact_router)


@app.get('/api/health')
def health():
    return {'ok': True}


@app.post('/api/admin/shutdown')
def admin_shutdown():
    """Trigger graceful shutdown: close SSE streams, abort active tasks, then terminate."""
    engine.event_bus.close_all()
    active_ids = list(execution._active.keys())
    aborted = []
    for task_id in active_ids:
        try:
            execution.abort_task(task_id)
            aborted.append(task_id)
        except Exception:
            pass

    my_pid = os.getpid()
    # If launched via uvicorn --reload, the worker's parent is the reloader and
    # must be terminated too; otherwise getppid() is the shell/CLI and we
    # should not touch it.
    parent_pid = os.getppid() if os.environ.get('AMC_DEV_RELOAD') else None
    is_windows = sys.platform == 'win32'

    def _terminate(pid: int) -> None:
        try:
            if is_windows:
                # On Windows os.kill maps to TerminateProcess; use taskkill /T
                # to also reap any child processes uvicorn may have spawned.
                import subprocess as _sp
                _sp.run(
                    ['taskkill', '/F', '/T', '/PID', str(pid)],
                    capture_output=True,
                )
            else:
                os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    def _deferred_kill():
        import time as _time
        _time.sleep(0.5)
        if parent_pid:
            _terminate(parent_pid)
        _terminate(my_pid)

    import threading
    threading.Thread(target=_deferred_kill, daemon=True).start()
    return {'ok': True, 'aborted_tasks': aborted}


STATIC_DIR = Path(__file__).resolve().parent.parent / 'static'
BUNDLED_UI_DIR = get_bundled_ui_dir()
STATIC_INDEX = (
    BUNDLED_UI_DIR / 'index.html'
    if (BUNDLED_UI_DIR / 'index.html').is_file()
    else STATIC_DIR / 'index.html'
)

if (BUNDLED_UI_DIR / 'assets').is_dir():
    app.mount('/assets', StaticFiles(directory=str(BUNDLED_UI_DIR / 'assets')), name='ui-assets')


def _resolve_static_index() -> Path:
    bundled = BUNDLED_UI_DIR / 'index.html'
    if bundled.is_file():
        return bundled
    return STATIC_DIR / 'index.html'


@app.get('/')
def root():
    return FileResponse(_resolve_static_index())


@app.get('/{full_path:path}')
def spa_fallback(full_path: str):
    if full_path == 'api' or full_path.startswith('api/'):
        raise HTTPException(status_code=404, detail='Not Found')
    # Serve top-level bundled static files safely (no directory traversal).
    if full_path and BUNDLED_UI_DIR.is_dir():
        ui_root = BUNDLED_UI_DIR.resolve()
        candidate = (ui_root / full_path).resolve()
        try:
            candidate.relative_to(ui_root)
        except ValueError:
            raise HTTPException(status_code=404, detail='Not Found')
        if candidate.is_file():
            return FileResponse(candidate)
    return FileResponse(_resolve_static_index())
