from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from app.core.context_compiler import DEFAULT_REVIEW_LANGUAGE, normalize_review_language
from app.core.execution import (
    BackendUnavailableError,
    ExecutionService,
    PullRequestRefreshError,
)


def get_execution() -> ExecutionService:
    from app.api.app import execution
    return execution


router = APIRouter(prefix='/api/tasks', tags=['control'])


class StartTaskRequest(BaseModel):
    review_note: str | None = None
    prompt: str | None = None
    language: str | None = DEFAULT_REVIEW_LANGUAGE

    @field_validator('language')
    @classmethod
    def _validate_language(cls, value: str | None) -> str:
        return normalize_review_language(value, strict=True)


@router.post('/{task_id}/start')
def start_task(task_id: str, request: StartTaskRequest, execution: ExecutionService = Depends(get_execution)):
    try:
        run = execution.start_task(
            task_id,
            prompt=request.prompt,
            review_note=request.review_note,
            language=request.language,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BackendUnavailableError as exc:
        # The task exists; the server just has no adapter for its backend.
        # Answering 404 here sends the user looking for a missing task.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PullRequestRefreshError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return run.model_dump()


@router.post('/{task_id}/abort')
def abort_task(task_id: str, execution: ExecutionService = Depends(get_execution)):
    ok = execution.abort_task(task_id)
    if not ok:
        raise HTTPException(status_code=409, detail='task is not actively running')
    return {'ok': True}


@router.post('/{task_id}/cleanup-worktree')
def cleanup_worktree(task_id: str, execution: ExecutionService = Depends(get_execution)):
    try:
        return execution.cleanup_task_worktree(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
