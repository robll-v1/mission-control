import inspect

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from app.core.config import UNSET
from app.core.context_compiler import DEFAULT_REVIEW_LANGUAGE, normalize_review_language
from app.core.execution import (
    BackendUnavailableError,
    ExecutionService,
    PullRequestRefreshError,
)
from app.services.verification_service import VerificationService


def get_execution() -> ExecutionService:
    from app.api.app import execution
    return execution


def get_verification() -> VerificationService:
    from app.api.app import verification
    return verification


router = APIRouter(prefix='/api/tasks', tags=['control'])


class StartTaskRequest(BaseModel):
    review_note: str | None = None
    review_focus: str | None = None
    prompt: str | None = None
    backend: str | None = None
    agent: str | None = None
    model: str | None = None
    reasoning: str | None = None
    language: str | None = DEFAULT_REVIEW_LANGUAGE

    @field_validator('language')
    @classmethod
    def _validate_language(cls, value: str | None) -> str:
        return normalize_review_language(value, strict=True)


@router.post('/{task_id}/start')
def start_task(task_id: str, request: StartTaskRequest, execution: ExecutionService = Depends(get_execution)):
    fields = request.model_fields_set
    # Explicit `null` in JSON forces CLI/Model default for this round; omitting
    # the field inherits from review / repo / global config.
    if 'backend' in fields or 'agent' in fields:
        req_backend = request.backend if 'backend' in fields else request.agent
    else:
        req_backend = UNSET
    req_model = request.model if 'model' in fields else UNSET
    req_reasoning = request.reasoning if 'reasoning' in fields else UNSET

    params = inspect.signature(execution.start_task).parameters
    accepts_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    supports_run_settings = accepts_varkw or 'backend' in params

    kwargs: dict[str, object] = {
        'prompt': request.prompt,
        'review_note': request.review_note,
        'language': (request.language if 'language' in fields else UNSET) if supports_run_settings else request.language,
    }
    if accepts_varkw or 'review_focus' in params:
        kwargs['review_focus'] = request.review_focus
    if accepts_varkw or 'backend' in params:
        kwargs['backend'] = req_backend
    if accepts_varkw or 'model' in params:
        kwargs['model'] = req_model
    if accepts_varkw or 'reasoning' in params:
        kwargs['reasoning'] = req_reasoning

    try:
        run = execution.start_task(task_id, **kwargs)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except BackendUnavailableError as exc:
        # The task exists; the server just has no adapter for its backend.
        # Answering 404 here sends the user looking for a missing task.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PullRequestRefreshError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return run.model_dump()


class VerifyFindingsRequest(BaseModel):
    backend: str | None = None
    model: str | None = None
    reasoning: str | None = None
    finding_ids: list[str] | None = None
    language: str | None = DEFAULT_REVIEW_LANGUAGE

    @field_validator('language')
    @classmethod
    def _validate_language(cls, value: str | None) -> str:
        return normalize_review_language(value, strict=True)


@router.post('/{task_id}/verify')
def verify_task_findings(
    task_id: str,
    request: VerifyFindingsRequest | None = None,
    verification: VerificationService = Depends(get_verification),
):
    req = request or VerifyFindingsRequest()
    try:
        return verification.start_verification(
            task_id,
            backend=req.backend,
            model=req.model,
            reasoning=req.reasoning,
            finding_ids=req.finding_ids,
            language=req.language or DEFAULT_REVIEW_LANGUAGE,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post('/{task_id}/abort')
def abort_task(
    task_id: str,
    execution: ExecutionService = Depends(get_execution),
    verification: VerificationService = Depends(get_verification),
):
    ok = execution.abort_task(task_id) or verification.abort_verification(task_id)
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
