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


_prune_startup_worktrees()
app = FastAPI(title='PR Review Control')
app.include_router(task_router)
app.include_router(stream_router)
app.include_router(control_router)
app.include_router(artifact_router)


@app.get('/api/health')
def health():
    return {'ok': True}


STATIC_INDEX = Path(__file__).resolve().parent.parent / 'static' / 'index.html'


@app.get('/')
def root():
    return FileResponse(STATIC_INDEX)
