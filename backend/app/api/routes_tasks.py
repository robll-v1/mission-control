from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from app.core.context_compiler import ContextCompiler
from app.core.github_ingest import fetch_pull_request, is_github_pr_url
from app.core.mission_engine import MissionEngine
from app.core.models import TaskStatus
from app.schemas.task import CreateTaskRequest
from app.services.artifact_store import ArtifactStore
from app.services.review_result_service import ReviewResultService


def get_engine() -> MissionEngine:
    from app.api.app import engine
    return engine


def get_artifacts() -> ArtifactStore:
    from app.api.app import artifacts
    return artifacts


def get_contexts() -> ContextCompiler:
    from app.api.app import contexts
    return contexts


router = APIRouter(prefix='/api/tasks', tags=['tasks'])


@router.get('')
def list_tasks(mission: MissionEngine = Depends(get_engine)):
    tasks = [ReviewResultService.backfill_task(db=mission.db, task=task) for task in mission.db.list_tasks()]
    return [task.model_dump() for task in tasks]


@router.post('')
def create_task(
    request: CreateTaskRequest,
    mission: MissionEngine = Depends(get_engine),
    artifact_store: ArtifactStore = Depends(get_artifacts),
    contexts: ContextCompiler = Depends(get_contexts),
):
    repo_path = request.repo_path.strip()
    if not repo_path:
        raise HTTPException(status_code=400, detail='repo_path is required')
    if not Path(repo_path).exists():
        raise HTTPException(status_code=400, detail=f'repo_path does not exist: {repo_path}')

    pr_url = request.pr_url.strip()
    if not pr_url:
        raise HTTPException(status_code=400, detail='pr_url is required')
    if not is_github_pr_url(pr_url):
        raise HTTPException(status_code=400, detail='pr_url must be a GitHub pull request URL')

    review_focus = request.review_focus.strip()
    try:
        pull_request = fetch_pull_request(pr_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    title = pull_request.task_title
    description = pull_request.to_description(review_focus=review_focus)
    raw_pr = pull_request.raw_pr

    task = mission.create_task(
        title=title,
        repo_path=repo_path,
        description=description,
        source_type='pull_request',
        source_url=pr_url,
        backend=request.backend,
        review_focus=review_focus,
        pr_owner=pull_request.owner,
        pr_repo=pull_request.repo,
        pr_number=pull_request.pr_number,
        pr_head_ref=str(raw_pr.get('head', {}).get('ref') or '') or None,
        pr_head_sha=str(raw_pr.get('head', {}).get('sha') or '') or None,
        pr_base_ref=str(raw_pr.get('base', {}).get('ref') or '') or None,
        review_paths=pull_request.changed_files,
    )

    pr_json_path = artifact_store.write_text(task.id, 'context/pull_request.json', pull_request.to_json())
    pr_md_path = artifact_store.write_text(task.id, 'context/pull_request.md', description)
    mission.append_event(
        task_id=task.id,
        kind='task.pull_request_ingested',
        payload={
            'source_url': pr_url,
            'artifact_paths': [pr_json_path, pr_md_path],
            'changed_files': len(pull_request.changed_files),
            'reviews': len(pull_request.reviews),
            'comments': len(pull_request.issue_comments) + len(pull_request.review_comments),
        },
    )

    try:
        compiled = contexts.compile_task(task)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f'failed to compile review context: {exc}') from exc
    mission.append_event(
        task_id=task.id,
        kind='task.context_compiled',
        payload={
            'markdown_path': compiled.markdown_path,
            'json_path': compiled.json_path,
            'candidate_files': len(compiled.payload.get('candidate_files', [])),
            'changed_files': len(compiled.payload.get('review', {}).get('changed_files', [])),
        },
    )
    mission.set_stage(task.id, status=TaskStatus.CONTEXT_READY, stage='review_ready')
    return mission.db.get_task(task.id).model_dump()


@router.get('/{task_id}')
def get_task(
    task_id: str,
    mission: MissionEngine = Depends(get_engine),
    artifact_store: ArtifactStore = Depends(get_artifacts),
):
    task = mission.db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail='task not found')
    task = ReviewResultService.backfill_task(db=mission.db, task=task)
    events = mission.db.list_events(task_id)
    rounds = mission.db.list_runs(task_id)
    important_events = ReviewResultService.important_events(events)
    return {
        'task': task.model_dump(),
        'events': [event.model_dump() for event in events],
        'important_events': [event.model_dump() for event in important_events],
        'event_stats': {
            'all_count': len(events),
            'important_count': len(important_events),
            'hidden_count': max(len(events) - len(important_events), 0),
        },
        'latest_review_result': task.latest_review_result.model_dump() if task.latest_review_result is not None else None,
        'review_rounds': [run.model_dump() for run in rounds],
        'runs': [run.model_dump() for run in rounds],
        'checks': [check.model_dump() for check in mission.db.list_check_runs(task_id)],
        'artifacts': artifact_store.list_files(task_id),
    }
