import os
import shutil
import stat
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.core.context_compiler import ContextCompiler
from app.core.config import resolve_backend_config
from app.core.execution import ExecutionService
from app.core.finding_tracker import FindingTracker
from app.core.github_ingest import fetch_pull_request, is_github_pr_url
from app.core.mission_engine import MissionEngine
from app.core.models import FindingStatus, TaskStatus
from app.schemas.task import CreateTaskRequest
from app.services.artifact_store import ArtifactStore
from app.services.diff_service import DiffService
from app.services.editor_service import (
    EditorNotAllowedError,
    EditorUnavailableError,
    PathAccessError,
    open_in_editor,
    read_file_snippet,
)
from app.services.local_review_service import build_local_review_source
from app.services.review_result_service import ReviewResultService


class UpdateFindingStatusRequest(BaseModel):
    action: str | None = None
    status: str | None = None


class OpenInEditorRequest(BaseModel):
    path: str
    line: int | None = None
    editor: str | None = None


def get_engine() -> MissionEngine:
    from app.api.app import engine
    return engine


def get_artifacts() -> ArtifactStore:
    from app.api.app import artifacts
    return artifacts


def get_contexts() -> ContextCompiler:
    from app.api.app import contexts
    return contexts


def get_execution() -> ExecutionService:
    from app.api.app import execution
    return execution


def _generated_paths(repo_path: str, mission: MissionEngine, artifacts: ArtifactStore) -> list[str]:
    from app.api.app import worktrees as app_worktrees

    repo = Path(repo_path).resolve()
    worktrees_root = artifacts.runtime_root / 'worktrees'
    if not worktrees_root.is_absolute():
        worktrees_root = repo / worktrees_root
    paths = [
        (artifacts.runtime_root / 'tasks').resolve(),
        worktrees_root.resolve(),
        app_worktrees._worktrees_root_for_repo(repo_path).resolve(),
        *(p.resolve() for p in mission.db.generated_paths()),
    ]
    excluded: list[str] = []
    seen: set[str] = set()
    for path in paths:
        try:
            rel = path.relative_to(repo).as_posix()
        except ValueError:
            continue
        if rel not in seen:
            seen.add(rel)
            excluded.append(rel)
    return excluded


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

    review_focus = request.review_focus.strip()
    backend = resolve_backend_config(repo_path, backend=request.backend)['backend']
    task_model = (request.model or '').strip() or None
    task_reasoning = (request.reasoning or '').strip() or None
    task_language = request.language
    if request.source_type == 'pull_request':
        pr_url = (request.pr_url or '').strip()
        if not pr_url:
            raise HTTPException(status_code=400, detail='pr_url is required for pull_request tasks')
        if not is_github_pr_url(pr_url):
            raise HTTPException(status_code=400, detail='pr_url must be a GitHub pull request URL')
        try:
            pull_request = fetch_pull_request(pr_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        raw_pr = pull_request.raw_pr
        description = pull_request.to_description(review_focus=review_focus)
        task = mission.create_task(
            title=pull_request.task_title,
            repo_path=repo_path,
            description=description,
            source_type='pull_request',
            source_url=pr_url,
            backend=backend,
            model=task_model,
            reasoning=task_reasoning,
            language=task_language,
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
    else:
        try:
            local = build_local_review_source(
                repo_path,
                base=request.base,
                review_focus=review_focus,
                generated_paths=_generated_paths(repo_path, mission, artifact_store),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        task = mission.create_task(
            title=local.title,
            repo_path=repo_path,
            description=local.description,
            source_type='local_diff',
            source_url=None,
            backend=backend,
            model=task_model,
            reasoning=task_reasoning,
            language=task_language,
            review_focus=review_focus,
            pr_base_ref=local.base_branch,
            review_paths=local.changed_files,
            local_head_sha=local.head_sha,
            local_snapshot_sha=local.snapshot_sha,
        )
        diff_path = artifact_store.write_text(task.id, 'context/local_diff.patch', local.diff_text)
        mission.append_event(
            task_id=task.id,
            kind='task.local_diff_ingested',
            payload={
                'base_branch': local.base_branch,
                'artifact_paths': [diff_path],
                'changed_files': len(local.changed_files),
                'diff_size_bytes': len(local.diff_text.encode('utf-8')),
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
    tracked_findings = mission.db.list_findings(task_id)
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
        'findings': [finding.model_dump() for finding in tracked_findings],
        'finding_counts': FindingTracker.counts(tracked_findings),
        'review_rounds': [run.model_dump() for run in rounds],
        'runs': [run.model_dump() for run in rounds],
        'checks': [check.model_dump() for check in mission.db.list_check_runs(task_id)],
        'artifacts': artifact_store.list_files(task_id),
    }


@router.get('/{task_id}/findings')
def list_task_findings(
    task_id: str,
    status: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    mission: MissionEngine = Depends(get_engine),
):
    task = mission.db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail='task not found')
    ReviewResultService.backfill_task(db=mission.db, task=task)
    all_findings = mission.db.list_findings(task_id)
    filtered = all_findings
    if status:
        wanted = {s.strip().lower() for s in status.split(',') if s.strip()}
        if 'open' in wanted:
            wanted.discard('open')
            wanted.update({'new', 'persistent', 'unverified', 'likely_fixed'})
        filtered = [f for f in filtered if f.status.value in wanted]
    if severity:
        wanted_sev = {s.strip().lower() for s in severity.split(',') if s.strip()}
        filtered = [f for f in filtered if f.severity.lower() in wanted_sev]
    return {
        'findings': [f.model_dump() for f in filtered],
        'counts': FindingTracker.counts(all_findings),
    }


@router.patch('/{task_id}/findings/{finding_id}')
def update_task_finding(
    task_id: str,
    finding_id: str,
    request: UpdateFindingStatusRequest,
    mission: MissionEngine = Depends(get_engine),
):
    task = mission.db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail='task not found')
    ReviewResultService.backfill_task(db=mission.db, task=task)

    raw_action = request.action if request.action is not None else request.status
    if raw_action is None:
        # Explicit {"status": null} also acts as undo/reopen.
        if 'status' in request.model_fields_set and request.status is None:
            raw_action = 'reopen'
        else:
            raise HTTPException(
                status_code=400,
                detail="action or status is required ('resolve', 'dismiss', or 'reopen')",
            )

    try:
        updated = FindingTracker.set_user_status(
            db=mission.db,
            task_id=task_id,
            finding_id=finding_id,
            action=raw_action,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    mission.append_event(
        task_id=task_id,
        kind='finding.status_changed',
        payload={
            'finding_id': updated.id,
            'status': updated.status.value,
            'system_status': updated.system_status.value,
            'user_status': updated.user_status.value if updated.user_status is not None else None,
            'path': updated.path,
            'line': updated.line,
            'title': updated.title,
        },
    )
    all_findings = mission.db.list_findings(task_id)
    return {
        'finding': updated.model_dump(),
        'counts': FindingTracker.counts(all_findings),
    }


def _safe_task_artifact_root(artifact_store: ArtifactStore, task_id: str) -> Path | None:
    """Return the non-created task artifact directory if ``task_id`` is a safe single path segment."""
    if not task_id or task_id in ('.', '..') or '/' in task_id or '\\' in task_id or '\x00' in task_id:
        return None
    tasks_root = (artifact_store.runtime_root / 'tasks').resolve()
    entry = tasks_root / task_id
    if entry.parent != tasks_root:
        return None
    return entry


def _remove_task_artifact_entry(artifact_store: ArtifactStore, task_id: str) -> None:
    """Safely remove a task's own artifact directory without following symlinks."""
    entry = _safe_task_artifact_root(artifact_store, task_id)
    if entry is None:
        return
    try:
        lst = entry.lstat()
    except OSError:
        return
    if stat.S_ISLNK(lst.st_mode):
        # Remove only the symlink itself; never recursively delete its target.
        try:
            entry.unlink()
        except OSError:
            pass
        return
    if stat.S_ISDIR(lst.st_mode):
        shutil.rmtree(entry, ignore_errors=True)


@router.delete('/{task_id}')
def delete_task(
    task_id: str,
    mission: MissionEngine = Depends(get_engine),
    artifact_store: ArtifactStore = Depends(get_artifacts),
    execution: ExecutionService = Depends(get_execution),
):
    # Acquire the shared per-task exclusive slot so neither start_task nor
    # validation can race with worktree cleanup, DB deletion, or artifact removal.
    has_reserve = hasattr(execution, 'try_reserve_task') and hasattr(execution, 'release_task')
    if has_reserve:
        if not execution.try_reserve_task(task_id, 'deleting'):
            raise HTTPException(
                status_code=409,
                detail='cannot delete an active review; wait or cancel the running operation first',
            )
    elif execution.is_active(task_id):
        raise HTTPException(
            status_code=409,
            detail='cannot delete an active review; wait or cancel the running operation first',
        )

    try:
        task = mission.db.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail='task not found')
        if task.status == TaskStatus.VALIDATING:
            raise HTTPException(
                status_code=409,
                detail='cannot delete a review while validation checks are running',
            )

        # Clean up isolated worktree BEFORE deleting DB state; if cleanup fails or
        # leaves the worktree behind, abort deletion so the user can retry cleanly.
        if task.worktree_path:
            try:
                cleanup_res = execution.worktrees.cleanup_worktree(task)
            except Exception as exc:
                raise HTTPException(
                    status_code=500,
                    detail=f'failed to clean up review worktree: {exc}',
                ) from exc
            if isinstance(cleanup_res, dict) and cleanup_res.get('exists_after'):
                raise HTTPException(
                    status_code=500,
                    detail='failed to remove review worktree; delete aborted so you can retry',
                )

        deleted = mission.db.delete_task(task_id)
        if not deleted:
            raise HTTPException(status_code=404, detail='task not found')

        _remove_task_artifact_entry(artifact_store, task_id)
        return {'ok': True, 'deleted_task_id': task_id}
    finally:
        if has_reserve:
            execution.release_task(task_id, 'deleting')


@router.post('/{task_id}/open-in-editor')
def open_task_file_in_editor(
    task_id: str,
    request: OpenInEditorRequest,
    mission: MissionEngine = Depends(get_engine),
):
    task = mission.db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail='task not found')
    try:
        return open_in_editor(
            task.repo_path,
            request.path,
            line=request.line,
            editor=request.editor,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PathAccessError, EditorNotAllowedError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except EditorUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get('/{task_id}/snippet')
def get_task_file_snippet(
    task_id: str,
    path: str = Query(...),
    line: int = Query(..., ge=1),
    context: int = Query(default=2, ge=0, le=10),
    mission: MissionEngine = Depends(get_engine),
):
    task = mission.db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail='task not found')
    try:
        return read_file_snippet(task.repo_path, path, line=line, context=context)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PathAccessError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


_MAX_DIFF_RESPONSE_BYTES = 250_000


def _read_bounded_artifact_text(task_root: Path, rel_path: str, max_bytes: int) -> tuple[str, bool] | None:
    """Read up to ``max_bytes`` from a regular non-symlink artifact file strictly inside ``task_root``."""
    try:
        root_lst = task_root.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(root_lst.st_mode) or not stat.S_ISDIR(root_lst.st_mode):
        return None
    resolved_root = task_root.resolve()
    candidate = resolved_root / rel_path
    try:
        resolved_file = candidate.resolve(strict=True)
        resolved_file.relative_to(resolved_root)
    except (OSError, ValueError):
        return None

    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    fd = -1
    try:
        fd = os.open(str(resolved_file), flags)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        with os.fdopen(fd, 'rb') as handle:
            fd = -1
            raw = handle.read(max_bytes + 1)
    except OSError:
        return None
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass

    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[:max_bytes]
    return raw.decode('utf-8', errors='replace'), truncated


@router.get('/{task_id}/diff')
def get_task_diff(
    task_id: str,
    mission: MissionEngine = Depends(get_engine),
    artifact_store: ArtifactStore = Depends(get_artifacts),
):
    task = mission.db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail='task not found')
    diff_text = ''
    source = 'none'
    truncated = False
    task_root = _safe_task_artifact_root(artifact_store, task_id)
    if task_root is not None:
        for rel, label in (
            ('context/local_diff.patch', 'local_diff'),
            ('context/pr_diff.patch', 'pr_diff'),
            (DiffService.EXPORT_DIFF_REL, 'export_diff'),
        ):
            read_res = _read_bounded_artifact_text(task_root, rel, _MAX_DIFF_RESPONSE_BYTES)
            if read_res is not None:
                diff_text, truncated = read_res
                source = label
                break
    return {
        'task_id': task_id,
        'source': source,
        'changed_files': list(task.review_paths),
        'diff': diff_text,
        'truncated': truncated,
    }
