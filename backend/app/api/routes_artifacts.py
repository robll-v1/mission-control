from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.db import Database
from app.core.validation import ValidationService
from app.services.artifact_store import ArtifactStore
from app.services.diff_service import DiffService
from app.services.summary_service import SummaryService


def get_db() -> Database:
    from app.api.app import db
    return db


def get_validation() -> ValidationService:
    from app.api.app import validation
    return validation


def get_summaries() -> SummaryService:
    from app.api.app import summaries
    return summaries


def get_diffs() -> DiffService:
    from app.api.app import diffs
    return diffs


def get_artifacts() -> ArtifactStore:
    from app.api.app import artifacts
    return artifacts


router = APIRouter(prefix='/api/tasks', tags=['artifacts'])


class ValidateRequest(BaseModel):
    mode: str | None = None


@router.post('/{task_id}/validate')
def validate_task(task_id: str, request: ValidateRequest, validation: ValidationService = Depends(get_validation)):
    validation.start_validation(task_id, mode=request.mode)
    return {'ok': True}


@router.post('/{task_id}/export-summary')
def export_summary(task_id: str, summaries: SummaryService = Depends(get_summaries)):
    try:
        path = summaries.export_summary(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {'path': path}


@router.post('/{task_id}/export-diff')
def export_diff(task_id: str, db: Database = Depends(get_db), diffs: DiffService = Depends(get_diffs)):
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail='task not found')
    if not task.worktree_path:
        raise HTTPException(status_code=409, detail='task has no worktree')
    diff_path = diffs.export_diff(task_id=task.id, worktree_path=task.worktree_path)
    files_path = diffs.export_changed_files(task_id=task.id, worktree_path=task.worktree_path)
    return {'diff_path': diff_path, 'changed_files_path': files_path}


@router.get('/{task_id}/artifacts')
def list_artifacts(task_id: str, artifacts: ArtifactStore = Depends(get_artifacts)):
    task_dir = artifacts.task_dir(task_id)
    files = []
    for path in sorted(task_dir.rglob('*')):
        if path.is_file():
            files.append({'path': str(path), 'relative_path': str(path.relative_to(task_dir))})
    return files
