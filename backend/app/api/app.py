import logging
import os
import signal
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.adapters.opencode_adapter import OpenCodeAdapter
from app.api.routes_artifacts import router as artifact_router
from app.api.routes_control import router as control_router
from app.api.routes_stream import router as stream_router
from app.api.routes_tasks import router as task_router
from app.core.config import load_repo_config
from app.core.context_compiler import ContextCompiler
from app.core.db import Database
from app.core.execution import ExecutionService
from app.core.mission_engine import MissionEngine
from app.core.models import TaskStatus
from app.core.validation import ValidationService
from app.core.worktree import WorktreeManager
from app.services.artifact_store import ArtifactStore
from app.services.diff_service import DiffService
from app.services.summary_service import SummaryService

logger = logging.getLogger(__name__)


RUNTIME_ROOT = 'runtime'
db = Database('data/amc.db')
engine = MissionEngine(db)
adapters = {
    'opencode': OpenCodeAdapter(),
}
worktrees = WorktreeManager(RUNTIME_ROOT)
artifacts = ArtifactStore(RUNTIME_ROOT)
contexts = ContextCompiler(artifacts)
execution = ExecutionService(db=db, mission=engine, adapters=adapters, worktrees=worktrees, artifacts=artifacts, contexts=contexts)
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


def _recover_stale_tasks() -> None:
    """Mark tasks that were left in active states (from a previous crash) as FAILED."""
    stale_statuses = {TaskStatus.INGESTING, TaskStatus.RUNNING}
    tasks = db.list_tasks()
    recovered = 0
    for task in tasks:
        if task.status in stale_statuses:
            logger.warning(
                'Recovering stale task %s (was %s → marking FAILED)',
                task.id, task.status,
            )
            engine.set_stage(task.id, status=TaskStatus.FAILED, stage='interrupted')
            engine.append_event(
                task_id=task.id,
                run_id=None,
                kind='task.recovered',
                payload={
                    'previous_status': task.status,
                    'reason': 'Process terminated unexpectedly. Task was recovered on restart.',
                },
            )
            recovered += 1
    if recovered:
        logger.info('Recovered %d stale task(s)', recovered)


_prune_startup_worktrees()
_recover_stale_tasks()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Graceful shutdown: abort all running tasks before exit."""
    yield
    # ── shutdown ──
    active_ids = list(execution._active.keys())
    if active_ids:
        logger.info('Graceful shutdown: aborting %d active task(s)...', len(active_ids))
        for task_id in active_ids:
            try:
                execution.abort_task(task_id)
                logger.info('Aborted task %s', task_id)
            except Exception:
                logger.exception('Failed to abort task %s during shutdown', task_id)
    logger.info('Shutdown complete.')


app = FastAPI(title='PR Review Control', lifespan=lifespan)
app.include_router(task_router)
app.include_router(stream_router)
app.include_router(control_router)
app.include_router(artifact_router)


@app.get('/api/health')
def health():
    return {'ok': True}


@app.post('/api/admin/shutdown')
def admin_shutdown():
    """Trigger graceful shutdown: abort active tasks, then terminate."""
    active_ids = list(execution._active.keys())
    aborted = []
    for task_id in active_ids:
        try:
            execution.abort_task(task_id)
            aborted.append(task_id)
        except Exception:
            pass
    # Schedule SIGTERM to self after response is sent
    os.kill(os.getpid(), signal.SIGTERM)
    return {'ok': True, 'aborted_tasks': aborted}


STATIC_INDEX = Path(__file__).resolve().parent.parent / 'static' / 'index.html'


@app.get('/')
def root():
    return FileResponse(STATIC_INDEX)
