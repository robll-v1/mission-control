from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from app.core.context_compiler import ContextCompiler
from app.core.github_ingest import fetch_issue, is_github_issue_url
from app.core.mission_engine import MissionEngine
from app.core.models import TaskStatus
from app.schemas.task import CreateTaskRequest
from app.services.artifact_store import ArtifactStore


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
    return [task.model_dump() for task in mission.db.list_tasks()]


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

    title = request.title.strip()
    description = request.description.strip()
    source_type = request.source_type
    source_url = (request.source_url or '').strip() or None

    ingested_issue = None
    if source_url and is_github_issue_url(source_url):
        try:
            ingested_issue = fetch_issue(source_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        title = title or ingested_issue.task_title
        issue_description = ingested_issue.to_description()
        description = f'{issue_description}\n\n## Additional Notes\n{description}'.strip() if description else issue_description
        source_type = 'issue_url'

    if not title:
        raise HTTPException(status_code=400, detail='title is required unless a GitHub issue URL is provided')

    task = mission.create_task(
        title=title,
        repo_path=repo_path,
        description=description,
        source_type=source_type,
        source_url=source_url,
        backend=request.backend,
    )

    if ingested_issue is not None:
        issue_json_path = artifact_store.write_text(task.id, 'context/issue.json', ingested_issue.to_json())
        issue_md_path = artifact_store.write_text(task.id, 'context/issue.md', description)
        mission.append_event(
            task_id=task.id,
            kind='task.issue_ingested',
            payload={
                'source_url': source_url,
                'artifact_paths': [issue_json_path, issue_md_path],
                'comments': len(ingested_issue.comments),
            },
        )

    try:
        compiled = contexts.compile_task(task)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f'failed to compile task context: {exc}') from exc
    mission.append_event(
        task_id=task.id,
        kind='task.context_compiled',
        payload={
            'markdown_path': compiled.markdown_path,
            'json_path': compiled.json_path,
            'candidate_files': len(compiled.payload.get('candidate_files', [])),
            'recent_commits': len(compiled.payload.get('repo', {}).get('recent_commits', [])),
        },
    )
    mission.set_stage(task.id, status=TaskStatus.CONTEXT_READY, stage='context_ready')
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
    return {
        'task': task.model_dump(),
        'events': [event.model_dump() for event in mission.db.list_events(task_id)],
        'runs': [run.model_dump() for run in mission.db.list_runs(task_id)],
        'checks': [check.model_dump() for check in mission.db.list_check_runs(task_id)],
        'artifacts': artifact_store.list_files(task_id),
    }
