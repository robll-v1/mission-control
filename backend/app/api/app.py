from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.adapters.opencode_adapter import OpenCodeAdapter
from app.api.routes_artifacts import router as artifact_router
from app.api.routes_control import router as control_router
from app.api.routes_stream import router as stream_router
from app.api.routes_tasks import router as task_router
from app.core.context_compiler import ContextCompiler
from app.core.db import Database
from app.core.execution import ExecutionService
from app.core.mission_engine import MissionEngine
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
execution = ExecutionService(db=db, mission=engine, adapters=adapters, worktrees=worktrees, contexts=contexts)
validation = ValidationService(db=db, mission=engine, artifacts=artifacts)
diffs = DiffService(artifacts)
summaries = SummaryService(db, artifacts)
app = FastAPI(title='Agent Mission Control')
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
