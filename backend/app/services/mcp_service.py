"""Decoupled domain service for the Mission Control MCP Server.

Zero FastMCP, FastAPI, or stdio dependencies: every operation is a plain
Python method returning JSON-serializable dicts, backed by the shared SQLite
database, ExecutionService, and FindingTracker.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.adapters import AVAILABLE_BACKENDS, get_adapter
from app.core.backend_detector import BackendDetector
from app.core.config import UNSET, resolve_backend_config
from app.core.context_compiler import ContextCompiler, normalize_review_language
from app.core.db import Database
from app.core.execution import ExecutionService
from app.core.finding_tracker import FindingTracker
from app.core.github_ingest import fetch_pull_request, is_github_pr_url
from app.core.mission_engine import MissionEngine
from app.core.models import FindingStatus, Task, TaskStatus
from app.core.paths import get_db_path, get_runtime_dir, get_worktrees_dir, maybe_import_legacy_db
from app.core.proc import run_text
from app.core.reviewer_selection import normalize_agent_name, select_reviewer_backend
from app.core.worktree import WorktreeManager
from app.services.artifact_store import ArtifactStore
from app.services.local_review_service import build_local_review_source
from app.services.review_result_service import ReviewResultService

_ACTIVE_STATUSES = {TaskStatus.INGESTING, TaskStatus.RUNNING, TaskStatus.VALIDATING}
_SEVERITY_ORDER = {'critical': 4, 'high': 3, 'medium': 2, 'low': 1}
_MAX_WAIT_SEC = 45


@dataclass
class _LaunchRecord:
    task_id: str
    expected_round_index: int
    selected_backend: str
    selection_reason: str
    selection_note: str | None
    started_at: float = field(default_factory=time.time)
    finished: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    run_id: str | None = None
    error: str | None = None


def _error_payload(code: str, message: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'ok': False,
        'error': {'code': code, 'message': message},
    }
    payload.update(extra)
    return payload


def _static_probe_backend(
    backend_name: str,
    *,
    model: str | None = None,
    reasoning: str | None = None,
    adapter_factory: Callable[..., Any] = get_adapter,
    which_fn: Callable[[str], str | None] = shutil.which,
    run_text_fn: Callable[..., Any] = run_text,
) -> dict[str, Any]:
    """Run a non-LLM static probe for one backend without FastAPI coupling."""
    try:
        adapter = adapter_factory(backend_name, model=model, reasoning=reasoning)
    except KeyError as exc:
        return {
            'ok': False,
            'backend': backend_name,
            'executable': '',
            'message': str(exc),
        }
    except ValueError as exc:
        return {
            'ok': False,
            'backend': backend_name,
            'executable': '',
            'message': str(exc),
        }

    executable = adapter.executable_name()
    cmd = adapter.doctor_command()
    if not which_fn(executable):
        return {
            'ok': False,
            'backend': backend_name,
            'executable': executable,
            'command': cmd,
            'exit_code': None,
            'message': f'Executable `{executable}` was not found on PATH.',
        }

    try:
        res = run_text_fn(cmd, timeout=15)
    except subprocess.TimeoutExpired:
        return {
            'ok': False,
            'backend': backend_name,
            'executable': executable,
            'command': cmd,
            'exit_code': None,
            'message': f'Static probe timed out after 15s: {" ".join(cmd)}',
        }
    except OSError as exc:
        return {
            'ok': False,
            'backend': backend_name,
            'executable': executable,
            'command': cmd,
            'exit_code': None,
            'message': f'Failed to launch `{executable}`: {exc}',
        }

    stdout = (res.stdout or '').strip()
    stderr = (res.stderr or '').strip()
    detail = (stdout.splitlines()[0] if stdout else '') or (stderr.splitlines()[0] if stderr else '')
    if res.returncode != 0:
        return {
            'ok': False,
            'backend': backend_name,
            'executable': executable,
            'command': cmd,
            'exit_code': res.returncode,
            'message': stderr or stdout or f'Exited with code {res.returncode}',
        }
    return {
        'ok': True,
        'backend': backend_name,
        'executable': executable,
        'command': cmd,
        'exit_code': res.returncode,
        'message': detail or f'{backend_name} static check passed.',
    }


class McpReviewService:
    """Orchestrates Mission Control reviews for MCP tools over shared SQLite state.

    Coexistence invariant:
    - Does NOT acquire Web Workbench's ``DataDirLock``.
    - Does NOT run global ``recover_stale_tasks`` or ``prune_startup_worktrees``.
    - Tracks only tasks launched by this MCP process and cleans up only those
      on :meth:`shutdown`.
    """

    def __init__(
        self,
        *,
        db: Database,
        mission: MissionEngine,
        execution: ExecutionService,
        worktrees: WorktreeManager,
        artifacts: ArtifactStore,
        contexts: ContextCompiler,
        detect_all_fn: Callable[[], list[Any]] | None = None,
        detect_backend_fn: Callable[[str], Any] | None = None,
        probe_backend_fn: Callable[..., dict[str, Any]] | None = None,
        pr_fetcher: Callable[[str], Any] = fetch_pull_request,
        poll_interval_sec: float = 0.25,
    ):
        self.db = db
        self.mission = mission
        self.execution = execution
        self.worktrees = worktrees
        self.artifacts = artifacts
        self.contexts = contexts
        self._detect_all = detect_all_fn or BackendDetector.detect_all
        self._detect_backend = detect_backend_fn or BackendDetector.detect_backend
        self._probe_backend = probe_backend_fn or _static_probe_backend
        self._pr_fetcher = pr_fetcher
        self._poll_interval_sec = max(0.02, float(poll_interval_sec))

        self._lock = threading.Lock()
        self._launches: dict[str, _LaunchRecord] = {}
        self._owned_task_ids: set[str] = set()

    @classmethod
    def from_shared_paths(cls) -> McpReviewService:
        """Build a service connected to the shared platform SQLite DB & runtime."""
        checkout_root = Path(__file__).resolve().parent.parent.parent.parent
        extra_roots = [checkout_root] if (checkout_root / 'pyproject.toml').is_file() else []
        maybe_import_legacy_db(extra_roots=extra_roots)

        db_path = str(get_db_path())
        runtime_root = str(get_runtime_dir())
        worktrees_root = str(get_worktrees_dir())

        db = Database(db_path)
        mission = MissionEngine(db)
        adapters = {name: get_adapter(name) for name in AVAILABLE_BACKENDS}
        worktrees = WorktreeManager(runtime_root, worktrees_root=worktrees_root)
        artifacts = ArtifactStore(runtime_root)
        contexts = ContextCompiler(artifacts)
        execution = ExecutionService(
            db=db,
            mission=mission,
            adapters=adapters,
            worktrees=worktrees,
            artifacts=artifacts,
            contexts=contexts,
            adapter_factory=lambda name, *, model=None, variant=None, reasoning=None: get_adapter(
                name, model=model, variant=variant, reasoning=reasoning,
            ),
        )
        return cls(
            db=db,
            mission=mission,
            execution=execution,
            worktrees=worktrees,
            artifacts=artifacts,
            contexts=contexts,
        )

    def _generated_paths(self, repo_path: str) -> list[str]:
        repo = Path(repo_path).resolve()
        worktrees_root = self.artifacts.runtime_root / 'worktrees'
        if not worktrees_root.is_absolute():
            worktrees_root = repo / worktrees_root
        paths = [
            (self.artifacts.runtime_root / 'tasks').resolve(),
            worktrees_root.resolve(),
            self.worktrees._worktrees_root_for_repo(repo_path).resolve(),
            *(p.resolve() for p in self.db.generated_paths()),
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

    def _known_backends(self) -> set[str]:
        known = set(AVAILABLE_BACKENDS)
        known.update(self.execution.adapters.keys())
        return known

    # ── Tool 1: check_agents ─────────────────────────────────────────

    def check_agents(self, backend: str | None = None) -> dict[str, Any]:
        """Run non-LLM static detection and probes against local coding CLIs."""
        target = normalize_agent_name(backend)
        if target:
            if target not in self._known_backends():
                return _error_payload(
                    'unknown_backend',
                    f'Unknown backend {target!r}. Supported: {", ".join(sorted(self._known_backends()))}.',
                )
            cap = self._detect_backend(target)
            detected_caps = [cap] if cap is not None else []
            probe = self._probe_backend(target)
            entry = self._format_backend_check(target, cap, probe)
            return {
                'ok': bool(entry['ok']),
                'installed_backends': [target] if cap is not None else [],
                'backends': [entry],
            }

        detected_caps = list(self._detect_all())
        entries: list[dict[str, Any]] = []
        installed_names: list[str] = []
        for cap in detected_caps:
            installed_names.append(cap.name)
            probe = self._probe_backend(cap.name)
            entries.append(self._format_backend_check(cap.name, cap, probe))

        return {
            'ok': bool(entries) and all(bool(item['ok']) for item in entries),
            'installed_backends': installed_names,
            'backends': entries,
            'note': (
                None
                if entries
                else 'No supported coding agent CLI detected on PATH (codex, claude, opencode, gh copilot).'
            ),
        }

    @staticmethod
    def _format_backend_check(name: str, cap: Any, probe: dict[str, Any]) -> dict[str, Any]:
        return {
            'backend': name,
            'ok': bool(probe.get('ok')),
            'installed': cap is not None,
            'version': getattr(cap, 'version', '') if cap is not None else '',
            'executable': probe.get('executable', ''),
            'models': list(getattr(cap, 'models', []) or []) if cap is not None else [],
            'models_source': getattr(cap, 'models_source', 'unknown') if cap is not None else 'unknown',
            'models_call_verified': False,
            'reasoning_supported': bool(getattr(cap, 'reasoning_supported', False)) if cap is not None else False,
            'reasoning_levels': list(getattr(cap, 'reasoning_levels', []) or []) if cap is not None else [],
            'reasoning_default': getattr(cap, 'reasoning_default', None) if cap is not None else None,
            'model_reasoning': dict(getattr(cap, 'model_reasoning', {}) or {}) if cap is not None else {},
            'message': str(probe.get('message', '')),
        }

    # ── Tool 2: start_review ─────────────────────────────────────────

    def start_review(
        self,
        *,
        repo_path: str | None = None,
        task_id: str | None = None,
        pr_url: str | None = None,
        base: str | None = None,
        backend: str | None = None,
        exclude_agent: str | None = None,
        model: str | None = None,
        reasoning: str | None = None,
        focus: str | None = None,
        note: str | None = None,
        language: str = 'en',
    ) -> dict[str, Any]:
        """Create a review task (or continue an existing task_id) and start a round."""
        try:
            clean_language = normalize_review_language(language)
        except ValueError as exc:
            return _error_payload('invalid_language', str(exc))

        existing_task: Task | None = None
        clean_task_id = (task_id or '').strip() or None
        if clean_task_id:
            existing_task = self.db.get_task(clean_task_id)
            if existing_task is None:
                return _error_payload('task_not_found', f'Task not found: {clean_task_id}')
            if self.execution.is_active(clean_task_id):
                return _error_payload(
                    'round_already_active',
                    f'A review round is already active for task {clean_task_id}. Call wait_review or abort_review first.',
                    task_id=clean_task_id,
                )

        raw_repo = (repo_path or '').strip() or (existing_task.repo_path if existing_task else '')
        if not raw_repo:
            return _error_payload('invalid_repo_path', 'repo_path is required when starting a new review.')
        resolved_repo = Path(raw_repo).expanduser().resolve()
        if not resolved_repo.exists() or not resolved_repo.is_dir():
            return _error_payload('invalid_repo_path', f'repo_path does not exist or is not a directory: {raw_repo}')
        repo_str = str(resolved_repo)

        # Resolve reviewer backend using Option A cross-agent policy.
        configured_default: str | None = None
        if existing_task is not None and not exclude_agent:
            configured_default = existing_task.backend
        else:
            try:
                configured_default = str(resolve_backend_config(repo_str).get('backend') or '') or None
            except Exception:
                configured_default = None

        installed_names = [cap.name for cap in self._detect_all()]
        selection = select_reviewer_backend(
            requested=backend,
            exclude=exclude_agent,
            installed=installed_names,
            configured_default=configured_default,
        )
        if selection.backend is None:
            return _error_payload(
                'no_backend_available',
                selection.note or 'No supported coding agent CLI detected on PATH.',
                selection_reason=selection.reason,
            )
        if selection.backend not in self._known_backends():
            return _error_payload(
                'unknown_backend',
                f'Unknown backend {selection.backend!r}. Supported: {", ".join(sorted(self._known_backends()))}.',
                selection_reason=selection.reason,
            )

        clean_model = (model or '').strip() or None
        clean_reasoning = (reasoning or '').strip() or None
        clean_focus = (focus or '').strip()
        clean_note = (note or '').strip() or None
        clean_pr = (pr_url or '').strip() or None
        clean_base = (base or '').strip() or None

        if existing_task is not None:
            task = existing_task
        else:
            task_or_err = self._create_task(
                repo_path=repo_str,
                pr_url=clean_pr,
                base=clean_base,
                backend=selection.backend,
                model=clean_model,
                reasoning=clean_reasoning,
                language=clean_language,
                review_focus=clean_focus,
            )
            if isinstance(task_or_err, dict):
                return task_or_err
            task = task_or_err

        prior_runs = len(self.db.list_runs(task.id))
        expected_round = prior_runs + 1
        launch = _LaunchRecord(
            task_id=task.id,
            expected_round_index=expected_round,
            selected_backend=selection.backend,
            selection_reason=selection.reason,
            selection_note=selection.note,
        )

        with self._lock:
            existing_launch = self._launches.get(task.id)
            if existing_launch is not None and not existing_launch.finished.is_set():
                return _error_payload(
                    'round_already_active',
                    f'A review launch is already in progress for task {task.id}.',
                    task_id=task.id,
                )
            self._launches[task.id] = launch
            self._owned_task_ids.add(task.id)

        def _launch_worker() -> None:
            try:
                run = self.execution.start_task(
                    task.id,
                    review_note=clean_note,
                    language=clean_language,
                    backend=selection.backend,
                    model=clean_model if model is not None else UNSET,
                    reasoning=clean_reasoning if reasoning is not None else UNSET,
                    review_focus=clean_focus if focus is not None else None,
                )
                launch.run_id = run.id
            except BaseException as exc:
                launch.error = str(exc)
            finally:
                launch.finished.set()

        thread = threading.Thread(
            target=_launch_worker,
            name=f'amc-mcp-launch-{task.id[:8]}',
            daemon=True,
        )
        launch.thread = thread
        thread.start()

        # Brief wait to catch immediate synchronous errors (e.g. bad backend or slot conflict)
        # without ever blocking on a slow preflight probe.
        launch.finished.wait(timeout=0.15)
        if launch.finished.is_set() and launch.error is not None:
            latest = self.db.get_task(task.id)
            return _error_payload(
                'launch_failed',
                launch.error,
                task_id=task.id,
                status=latest.status.value if latest else 'failed',
                stage=latest.current_stage if latest else 'preflight_failed',
                selected_backend=selection.backend,
                selection_reason=selection.reason,
            )

        latest = self.db.get_task(task.id) or task
        return {
            'ok': True,
            'task_id': task.id,
            'round_index': expected_round,
            'status': latest.status.value,
            'stage': latest.current_stage,
            'source_type': latest.source_type,
            'snapshot_sha': latest.local_snapshot_sha or latest.pr_head_sha,
            'selected_backend': selection.backend,
            'selection_reason': selection.reason,
            'selection_note': selection.note,
            'next_step': f"Call wait_review(task_id='{task.id}') to wait for progress or findings.",
        }

    def _create_task(
        self,
        *,
        repo_path: str,
        pr_url: str | None,
        base: str | None,
        backend: str,
        model: str | None,
        reasoning: str | None,
        language: str,
        review_focus: str,
    ) -> Task | dict[str, Any]:
        if pr_url:
            if not is_github_pr_url(pr_url):
                return _error_payload('invalid_pr_url', 'pr_url must be a valid GitHub pull request URL.')
            try:
                pr = self._pr_fetcher(pr_url)
            except (ValueError, RuntimeError) as exc:
                return _error_payload('pr_ingest_failed', str(exc))
            raw_pr = pr.raw_pr
            description = pr.to_description(review_focus=review_focus)
            task = self.mission.create_task(
                title=pr.task_title,
                repo_path=repo_path,
                description=description,
                source_type='pull_request',
                source_url=pr_url,
                backend=backend,
                model=model,
                reasoning=reasoning,
                language=language,
                review_focus=review_focus,
                pr_owner=pr.owner,
                pr_repo=pr.repo,
                pr_number=pr.pr_number,
                pr_head_ref=str(raw_pr.get('head', {}).get('ref') or '') or None,
                pr_head_sha=str(raw_pr.get('head', {}).get('sha') or '') or None,
                pr_base_ref=str(raw_pr.get('base', {}).get('ref') or '') or None,
                review_paths=pr.changed_files,
            )
            pr_json_path = self.artifacts.write_text(task.id, 'context/pull_request.json', pr.to_json())
            pr_md_path = self.artifacts.write_text(task.id, 'context/pull_request.md', description)
            self.mission.append_event(
                task_id=task.id,
                kind='task.pull_request_ingested',
                payload={
                    'source_url': pr_url,
                    'artifact_paths': [pr_json_path, pr_md_path],
                    'changed_files': len(pr.changed_files),
                },
            )
        else:
            try:
                local = build_local_review_source(
                    repo_path,
                    base=base,
                    review_focus=review_focus,
                    generated_paths=self._generated_paths(repo_path),
                )
            except ValueError as exc:
                return _error_payload('local_ingest_failed', str(exc))
            task = self.mission.create_task(
                title=local.title,
                repo_path=repo_path,
                description=local.description,
                source_type='local_diff',
                source_url=None,
                backend=backend,
                model=model,
                reasoning=reasoning,
                language=language,
                review_focus=review_focus,
                pr_base_ref=local.base_branch,
                review_paths=local.changed_files,
                local_head_sha=local.head_sha,
                local_snapshot_sha=local.snapshot_sha,
            )
            diff_path = self.artifacts.write_text(task.id, 'context/local_diff.patch', local.diff_text)
            self.mission.append_event(
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
            compiled = self.contexts.compile_task(task)
        except Exception as exc:
            return _error_payload('context_compile_failed', f'Failed to compile review context: {exc}')

        self.mission.append_event(
            task_id=task.id,
            kind='task.context_compiled',
            payload={
                'markdown_path': compiled.markdown_path,
                'json_path': compiled.json_path,
                'candidate_files': len(compiled.payload.get('candidate_files', [])),
                'changed_files': len(compiled.payload.get('review', {}).get('changed_files', [])),
            },
        )
        self.mission.set_stage(task.id, status=TaskStatus.CONTEXT_READY, stage='review_ready')
        return self.db.get_task(task.id) or task

    # ── Tool 3: wait_review ──────────────────────────────────────────

    def _is_round_still_running(self, task_id: str, task: Task) -> bool:
        with self._lock:
            launch = self._launches.get(task_id)
        if launch is not None and not launch.finished.is_set():
            return True
        if self.execution.is_active(task_id):
            return True
        return task.status in _ACTIVE_STATUSES

    def wait_review(
        self,
        *,
        task_id: str,
        wait_sec: int = 25,
        min_severity: str | None = None,
    ) -> dict[str, Any]:
        """Bounded server-side long-poll for review progress or final findings."""
        clean_id = (task_id or '').strip()
        if not clean_id:
            return _error_payload('invalid_task_id', 'task_id is required.')

        clamped_wait = max(0, min(int(wait_sec), _MAX_WAIT_SEC))
        min_level = 1
        if min_severity:
            sev_key = min_severity.strip().lower()
            if sev_key not in _SEVERITY_ORDER:
                return _error_payload(
                    'invalid_severity',
                    f'Invalid min_severity {min_severity!r}. Expected one of: critical, high, medium, low.',
                )
            min_level = _SEVERITY_ORDER[sev_key]

        task = self.db.get_task(clean_id)
        if task is None:
            return _error_payload('task_not_found', f'Task not found: {clean_id}')

        deadline = time.monotonic() + clamped_wait
        while self._is_round_still_running(clean_id, task):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(self._poll_interval_sec, remaining))
            latest = self.db.get_task(clean_id)
            if latest is None:
                return _error_payload('task_not_found', f'Task was deleted: {clean_id}')
            task = latest

        with self._lock:
            launch = self._launches.get(clean_id)

        still_running = self._is_round_still_running(clean_id, task)
        runs = self.db.list_runs(clean_id)
        latest_run = runs[-1] if runs else None
        events = self.db.list_events(clean_id)
        important = ReviewResultService.important_events(events)

        if still_running:
            started_at = (
                latest_run.started_at
                if latest_run is not None
                else (launch.started_at if launch is not None else task.updated_at)
            )
            return {
                'ok': True,
                'done': False,
                'task_id': clean_id,
                'status': task.status.value,
                'stage': task.current_stage,
                'round_index': (
                    latest_run.round_index
                    if latest_run is not None
                    else (launch.expected_round_index if launch is not None else len(runs) + 1)
                ),
                'backend': (
                    latest_run.backend
                    if latest_run is not None
                    else (launch.selected_backend if launch is not None else task.backend)
                ),
                'elapsed_sec': round(max(0.0, time.time() - started_at), 1),
                'recent_events': [
                    {'seq': ev.seq, 'kind': ev.kind, 'level': ev.level.value}
                    for ev in important[-5:]
                ],
                'next_step': f"Call wait_review(task_id='{clean_id}') again.",
            }

        task = ReviewResultService.backfill_task(db=self.db, task=task)
        tracked_findings = self.db.list_findings(clean_id)
        counts = FindingTracker.counts(tracked_findings)
        filtered = [
            f for f in tracked_findings
            if _SEVERITY_ORDER.get(f.severity.lower(), 0) >= min_level
        ]
        result = task.latest_review_result

        error_detail: str | None = None
        if task.status == TaskStatus.FAILED:
            if launch is not None and launch.error:
                error_detail = launch.error
            else:
                for ev in reversed(events):
                    if ev.kind == 'error' or ev.level.value == 'error':
                        msg = ev.payload.get('message') or ev.payload.get('error')
                        if msg:
                            error_detail = str(msg)
                            break
                if error_detail is None:
                    error_detail = f'Review failed at stage {task.current_stage}.'

        verdict: str
        passed: bool
        if task.status == TaskStatus.FAILED:
            verdict = 'failed'
            passed = False
        elif task.status == TaskStatus.ABORTED:
            verdict = 'inconclusive'
            passed = False
        elif result is not None:
            verdict = result.verdict.value
            blocking_open = (
                counts.get('new', 0)
                + counts.get('persistent', 0)
                + counts.get('unverified', 0)
            )
            passed = verdict == 'clear' and blocking_open == 0
        else:
            verdict = 'inconclusive'
            passed = False

        return {
            'ok': task.status != TaskStatus.FAILED,
            'done': True,
            'task_id': clean_id,
            'status': task.status.value,
            'stage': task.current_stage,
            'round_index': latest_run.round_index if latest_run is not None else 0,
            'backend': latest_run.backend if latest_run is not None else task.backend,
            'model': latest_run.model if latest_run is not None else task.model,
            'reasoning': latest_run.reasoning if latest_run is not None else task.reasoning,
            'selection_reason': launch.selection_reason if launch is not None else None,
            'selection_note': launch.selection_note if launch is not None else None,
            'verdict': verdict,
            'passed': passed,
            'summary': result.summary if result is not None else '',
            'finding_counts': counts,
            'findings': [
                {
                    'id': f.id,
                    'severity': f.severity,
                    'status': f.status.value,
                    'system_status': f.system_status.value,
                    'user_status': f.user_status.value if f.user_status is not None else None,
                    'category': f.category,
                    'path': f.path,
                    'line': f.line,
                    'title': f.title,
                    'detail': f.detail,
                    'first_round': f.first_round,
                    'last_round': f.last_round,
                }
                for f in filtered
            ],
            'error': error_detail,
        }

    # ── Tool 4: update_finding ───────────────────────────────────────

    def update_finding(
        self,
        *,
        task_id: str,
        finding_id: str,
        action: str,
    ) -> dict[str, Any]:
        """Update a finding's human disposition (resolve, dismiss, or reopen) in SQLite."""
        clean_task_id = (task_id or '').strip()
        clean_finding_id = (finding_id or '').strip()
        clean_action = (action or '').strip().lower()
        if not clean_task_id or not clean_finding_id:
            return _error_payload('invalid_arguments', 'task_id and finding_id are required.')

        task = self.db.get_task(clean_task_id)
        if task is None:
            return _error_payload('task_not_found', f'Task not found: {clean_task_id}')
        ReviewResultService.backfill_task(db=self.db, task=task)

        try:
            updated = FindingTracker.set_user_status(
                db=self.db,
                task_id=clean_task_id,
                finding_id=clean_finding_id,
                action=clean_action,
            )
        except KeyError as exc:
            return _error_payload('finding_not_found', str(exc))
        except ValueError as exc:
            return _error_payload('invalid_action', str(exc))

        self.mission.append_event(
            task_id=clean_task_id,
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
        all_findings = self.db.list_findings(clean_task_id)
        return {
            'ok': True,
            'task_id': clean_task_id,
            'finding': {
                'id': updated.id,
                'severity': updated.severity,
                'status': updated.status.value,
                'system_status': updated.system_status.value,
                'user_status': updated.user_status.value if updated.user_status is not None else None,
                'path': updated.path,
                'line': updated.line,
                'title': updated.title,
            },
            'finding_counts': FindingTracker.counts(all_findings),
        }

    # ── Tool 5: abort_review ─────────────────────────────────────────

    def abort_review(self, *, task_id: str) -> dict[str, Any]:
        """Abort an in-flight preflight/review round and terminate its process tree."""
        clean_id = (task_id or '').strip()
        if not clean_id:
            return _error_payload('invalid_task_id', 'task_id is required.')

        task = self.db.get_task(clean_id)
        if task is None:
            return _error_payload('task_not_found', f'Task not found: {clean_id}')

        with self._lock:
            launch = self._launches.get(clean_id)

        # If a launch thread was just spawned and hasn't registered its slot yet,
        # wait briefly so abort lands cleanly on the ExecutionService reservation.
        if launch is not None and not launch.finished.is_set() and not self.execution.is_active(clean_id):
            deadline = time.monotonic() + 0.5
            while (
                not launch.finished.is_set()
                and not self.execution.is_active(clean_id)
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)

        aborted = self.execution.abort_task(clean_id)
        if aborted and launch is not None:
            launch.finished.wait(timeout=3.0)

        latest = self.db.get_task(clean_id) or task
        return {
            'ok': True,
            'task_id': clean_id,
            'aborted': aborted,
            'status': latest.status.value,
            'stage': latest.current_stage,
        }

    # ── Lifecycle teardown ───────────────────────────────────────────

    def shutdown(self) -> None:
        """Abort only active reviews launched by this MCP server process."""
        with self._lock:
            owned = list(self._owned_task_ids)
            launches = [self._launches[tid] for tid in owned if tid in self._launches]
        for tid in owned:
            try:
                if self.execution.is_active(tid):
                    self.execution.abort_task(tid)
            except Exception:
                pass
        for launch in launches:
            if launch.thread is not None and launch.thread.is_alive():
                launch.thread.join(timeout=2.0)
