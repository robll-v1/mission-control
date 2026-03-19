from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.execution import ExecutionService, PullRequestRefreshError


def get_execution() -> ExecutionService:
    from app.api.app import execution
    return execution


router = APIRouter(prefix='/api/tasks', tags=['control'])


class StartTaskRequest(BaseModel):
    review_note: str | None = None
    prompt: str | None = None


@router.post('/{task_id}/start')
def start_task(task_id: str, request: StartTaskRequest, execution: ExecutionService = Depends(get_execution)):
    try:
        run = execution.start_task(task_id, prompt=request.prompt, review_note=request.review_note)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
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
