"""
ReviewEngine — High-level SDK for Agent Skill integration.

Usage:
    from app.sdk import ReviewEngine

    engine = ReviewEngine()
    result = engine.review("/path/to/repo", pr_url="https://github.com/.../pull/123")
    print(result.verdict, result.findings)
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from app.adapters import get_adapter, AVAILABLE_BACKENDS
from app.core.context_compiler import ContextCompiler
from app.core.db import Database
from app.core.execution import ExecutionService
from app.core.github_ingest import fetch_pull_request, is_github_pr_url
from app.core.mission_engine import MissionEngine
from app.core.models import (
    Event,
    ReviewFinding,
    ReviewResult,
    ReviewVerdict,
    Run,
    Task,
    TaskStatus,
)
from app.core.proc import run_text
from app.core.validation import ValidationService
from app.core.worktree import WorktreeManager
from app.services.artifact_store import ArtifactStore
from app.services.local_review_service import (
    build_local_review_source,
    get_commit_messages,
    get_local_changed_files,
    get_local_diff,
    resolve_base_branch,
)
from app.services.review_history import (
    FindingStatus,
    ReviewHistoryStore,
    ReviewRecord,
    StoredFinding,
)
from app.services.review_result_service import ReviewResultService


@dataclass
class ReviewReport:
    """Structured output from a complete review session."""
    task_id: str
    verdict: ReviewVerdict
    passed: bool
    findings: list[ReviewFinding] = field(default_factory=list)
    finding_count: int = 0
    severity_counts: dict[str, int] = field(default_factory=dict)
    summary: str = ''
    rounds_executed: int = 0
    duration_sec: float = 0.0
    can_continue: bool = False
    error: dict | None = None
    # Incremental review fields
    finding_statuses: list[FindingStatus] = field(default_factory=list)
    resolved_findings: list[StoredFinding] = field(default_factory=list)
    is_incremental: bool = False
    previous_sha: str | None = None
    inferred_focus: str = ''
    metrics: dict[str, object] = field(default_factory=dict)


class ReviewEngine:
    """High-level SDK wrapping mission-control's core engine.

    Provides a clean interface for:
    - Creating review tasks (PR or local-diff mode)
    - Starting and monitoring review rounds
    - Extracting structured findings
    - One-shot review convenience method
    """

    def __init__(
        self,
        *,
        db_path: str = 'data/amc.db',
        runtime_root: str = 'runtime',
        language: str = 'en',
        backend: str = 'opencode',
        model: str | None = None,
    ):
        self.language = language
        self.backend_name = backend

        # Model resolution: explicit param > env var > .amc.yaml > None (use backend default)
        self.model = model or self._resolve_model()

        self._db = Database(db_path)
        self._engine = MissionEngine(self._db)
        # direct_api backend doesn't use subprocess adapters
        if backend == 'direct_api':
            self._adapters = {}
        else:
            self._adapters = {self.backend_name: get_adapter(self.backend_name, model=self.model)}
        self._worktrees = WorktreeManager(runtime_root)
        self._artifacts = ArtifactStore(runtime_root)
        self._contexts = ContextCompiler(self._artifacts)
        self._execution = ExecutionService(
            db=self._db,
            mission=self._engine,
            adapters=self._adapters,
            worktrees=self._worktrees,
            artifacts=self._artifacts,
            contexts=self._contexts,
        )
        self._validation = ValidationService(
            db=self._db,
            mission=self._engine,
            artifacts=self._artifacts,
        )

    def _resolve_model(self, repo_path: str = '.') -> str | None:
        """Resolve the model from env var or merged config.

        Priority: ``$AMC_MODEL`` > ``backend.<backend>.model`` >
        ``backend.<default>.model`` > ``None`` (let the backend decide).

        This used to be declared ``@staticmethod`` while its body dereferenced
        ``self``; the resulting ``NameError`` was swallowed by a bare
        ``except Exception``, so configured models were silently ignored and
        only ``--model`` / ``$AMC_MODEL`` ever took effect.
        """
        import os

        env_model = os.environ.get('AMC_MODEL', '').strip()
        if env_model:
            return env_model

        from app.core.config import load_repo_config
        config = load_repo_config(repo_path)
        backend_config = config.get('backend', {})
        if not isinstance(backend_config, dict):
            return None

        def _model_of(key: str) -> str:
            section = backend_config.get(key)
            if not isinstance(section, dict):
                return ''
            return str(section.get('model') or '').strip()

        # Backend-specific first, then whatever the configured default backend uses.
        return (
            _model_of(self.backend_name)
            or _model_of(str(backend_config.get('default', 'opencode')))
            or None
        )

    # ------------------------------------------------------------------
    # Task creation
    # ------------------------------------------------------------------

    def create_task(
        self,
        repo_path: str,
        *,
        pr_url: str | None = None,
        base: str | None = None,
        review_focus: str = '',
        context: dict | None = None,
    ) -> Task:
        """Create a review task.

        If pr_url is provided, uses PR mode (fetches from GitHub API).
        If pr_url is None, uses local-diff mode (compares against base branch).
        """
        repo = Path(repo_path).resolve()
        if not repo.exists():
            raise ValueError(f'repo_path does not exist: {repo_path}')

        if pr_url:
            return self._create_pr_task(str(repo), pr_url, review_focus)
        else:
            return self._create_local_task(str(repo), base, review_focus, context)

    def _create_pr_task(self, repo_path: str, pr_url: str, review_focus: str) -> Task:
        """Create task from a GitHub PR URL."""
        if not is_github_pr_url(pr_url):
            raise ValueError(f'Invalid GitHub PR URL: {pr_url}')

        pull_request = fetch_pull_request(pr_url)
        title = pull_request.task_title
        description = pull_request.to_description(review_focus=review_focus)
        raw_pr = pull_request.raw_pr

        task = self._engine.create_task(
            title=title,
            repo_path=repo_path,
            description=description,
            source_type='pull_request',
            source_url=pr_url,
            backend=self.backend_name,
            review_focus=review_focus,
            pr_owner=pull_request.owner,
            pr_repo=pull_request.repo,
            pr_number=pull_request.pr_number,
            pr_head_ref=str(raw_pr.get('head', {}).get('ref') or '') or None,
            pr_head_sha=str(raw_pr.get('head', {}).get('sha') or '') or None,
            pr_base_ref=str(raw_pr.get('base', {}).get('ref') or '') or None,
            review_paths=pull_request.changed_files,
        )

        # Save PR artifacts
        self._artifacts.write_text(task.id, 'context/pull_request.json', pull_request.to_json())
        self._artifacts.write_text(task.id, 'context/pull_request.md', description)
        self._engine.append_event(
            task_id=task.id,
            kind='task.pull_request_ingested',
            payload={
                'source_url': pr_url,
                'changed_files': len(pull_request.changed_files),
            },
        )
        return task

    def _create_local_task(
        self,
        repo_path: str,
        base: str | None,
        review_focus: str,
        context: dict | None,
    ) -> Task:
        """Create task from local git diff (no PR URL needed)."""
        local = build_local_review_source(
            repo_path,
            base=base,
            review_focus=review_focus,
            context=context,
        )

        task = self._engine.create_task(
            title=local.title,
            repo_path=repo_path,
            description=local.description,
            source_type='local_diff',
            source_url=None,
            backend=self.backend_name,
            review_focus=review_focus,
            pr_base_ref=local.base_branch,
            review_paths=local.changed_files,
        )

        # Save local diff as artifact
        self._artifacts.write_text(task.id, 'context/local_diff.patch', local.diff_text)
        self._engine.append_event(
            task_id=task.id,
            kind='task.local_diff_ingested',
            payload={
                'base_branch': local.base_branch,
                'changed_files': len(local.changed_files),
                'diff_size_bytes': len(local.diff_text.encode('utf-8')),
            },
        )
        return task

    # ------------------------------------------------------------------
    # Review execution
    # ------------------------------------------------------------------

    def start_review(
        self,
        task_id: str,
        *,
        prompt: str | None = None,
        review_note: str | None = None,
    ) -> Run:
        """Start a review round. Returns immediately; review runs in background thread."""
        return self._execution.start_task(task_id, prompt=prompt, review_note=review_note, language=self.language)

    def poll_status(self, task_id: str) -> dict:
        """Get current task status and progress."""
        task = self._db.get_task(task_id)
        if task is None:
            raise KeyError(f'task not found: {task_id}')
        task = ReviewResultService.backfill_task(db=self._db, task=task)
        events = self._db.list_events(task_id)
        runs = self._db.list_runs(task_id)
        latest_event = events[-1] if events else None

        return {
            'task_id': task_id,
            'status': task.status,
            'stage': task.current_stage,
            'rounds': len(runs),
            'latest_event': latest_event.kind if latest_event else None,
            'verdict': task.latest_review_result.verdict if task.latest_review_result else None,
            'finding_count': task.latest_review_result.finding_count if task.latest_review_result else 0,
        }

    def wait_for_completion(self, task_id: str, *, timeout_sec: float = 600, poll_interval: float = 2.0) -> Task:
        """Block until task reaches a terminal state or timeout."""
        terminal_states = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.ABORTED, TaskStatus.WAITING_HUMAN}
        deadline = time.time() + timeout_sec

        while time.time() < deadline:
            task = self._db.get_task(task_id)
            if task is None:
                raise KeyError(f'task not found: {task_id}')
            task = ReviewResultService.backfill_task(db=self._db, task=task)
            if task.status in terminal_states:
                return task
            time.sleep(poll_interval)

        raise TimeoutError(f'Task {task_id} did not complete within {timeout_sec}s')

    def get_findings(self, task_id: str) -> list[ReviewFinding]:
        """Get structured findings from latest completed round."""
        task = self._db.get_task(task_id)
        if task is None:
            raise KeyError(f'task not found: {task_id}')
        task = ReviewResultService.backfill_task(db=self._db, task=task)
        if task.latest_review_result:
            return task.latest_review_result.findings
        return []

    def get_result(self, task_id: str) -> ReviewResult | None:
        """Get full ReviewResult from latest round."""
        task = self._db.get_task(task_id)
        if task is None:
            raise KeyError(f'task not found: {task_id}')
        task = ReviewResultService.backfill_task(db=self._db, task=task)
        return task.latest_review_result

    def stream_events(self, task_id: str, *, since_seq: int = 0) -> Iterator[Event]:
        """Yield events for a task since a given sequence number."""
        events = self._db.list_events(task_id)
        for event in events:
            if event.seq > since_seq:
                yield event

    def abort(self, task_id: str) -> bool:
        """Abort an active review."""
        return self._execution.abort_task(task_id)

    def validate(self, task_id: str, *, mode: str = 'standard') -> dict:
        """Run validation checks (build, test, lint)."""
        self._validation.start_validation(task_id, mode=mode)
        # Wait for validation to complete
        task = self.wait_for_completion(task_id, timeout_sec=300)
        checks = self._db.list_check_runs(task_id)
        return {
            'task_id': task_id,
            'status': task.status,
            'checks': [
                {
                    'name': c.name,
                    'status': c.status,
                    'exit_code': c.exit_code,
                    'duration_sec': c.duration_sec,
                }
                for c in checks
            ],
            'all_passed': all(c.status == 'passed' for c in checks),
        }

    def cleanup(self, task_id: str) -> dict:
        """Clean up worktree and resources for a task."""
        return self._execution.cleanup_task_worktree(task_id, reason='sdk_cleanup')

    # ------------------------------------------------------------------
    # Convenience: one-shot review
    # ------------------------------------------------------------------

    def review(
        self,
        repo_path: str,
        *,
        pr_url: str | None = None,
        base: str | None = None,
        review_focus: str = '',
        context: dict | None = None,
        max_rounds: int = 1,
        timeout_sec: float = 600,
        incremental: bool = True,
    ) -> ReviewReport:
        """One-shot review: create task → execute → return structured result.

        Args:
            repo_path: Path to local git repository.
            pr_url: GitHub PR URL (optional). If omitted, reviews local diff.
            base: Base branch for local-diff mode (default: auto-detect main/master).
            review_focus: What to focus the review on.
            context: Optional dict with intent, requirements, constraints.
            max_rounds: Maximum review rounds (default 1).
            timeout_sec: Per-round timeout in seconds.
            incremental: Use incremental review (skip already-reviewed code). Default True.

        Returns:
            ReviewReport with verdict, findings, and metadata.
        """
        start_time = time.time()
        repo = Path(repo_path).resolve()
        history = ReviewHistoryStore(str(repo))

        # --- Incremental: detect last reviewed SHA ---
        previous_sha = None
        is_incremental = False
        if incremental and not pr_url:
            previous_sha = history.get_last_reviewed_sha()
            if previous_sha:
                # Check if we have new commits since last review
                current_sha = self._get_head_sha(str(repo))
                if current_sha and current_sha != previous_sha:
                    is_incremental = True
                    # Use last reviewed SHA as base for incremental diff
                    base = previous_sha
                else:
                    is_incremental = False  # no new changes

        # --- Context auto-inference ---
        inferred_focus = review_focus
        if not review_focus and not pr_url:
            inferred_focus = self._infer_review_focus(str(repo), base or 'main')

        try:
            task = self.create_task(
                repo_path,
                pr_url=pr_url,
                base=base,
                review_focus=inferred_focus,
                context=context,
            )
        except Exception as exc:
            return ReviewReport(
                task_id='',
                verdict=ReviewVerdict.FAILED,
                passed=False,
                error={'code': 'create_failed', 'message': str(exc), 'recoverable': False},
            )

        rounds_executed = 0
        for round_idx in range(max_rounds):
            try:
                self.start_review(task.id)
                task = self.wait_for_completion(task.id, timeout_sec=timeout_sec)
                rounds_executed += 1
            except TimeoutError:
                self.abort(task.id)
                return ReviewReport(
                    task_id=task.id,
                    verdict=ReviewVerdict.FAILED,
                    passed=False,
                    rounds_executed=rounds_executed,
                    duration_sec=time.time() - start_time,
                    error={'code': 'timeout', 'message': f'Round {round_idx + 1} timed out after {timeout_sec}s', 'recoverable': True},
                )
            except Exception as exc:
                return ReviewReport(
                    task_id=task.id,
                    verdict=ReviewVerdict.FAILED,
                    passed=False,
                    rounds_executed=rounds_executed,
                    duration_sec=time.time() - start_time,
                    error={'code': 'execution_error', 'message': str(exc), 'recoverable': False},
                )

            # Check result
            result = self.get_result(task.id)
            if result and result.verdict == ReviewVerdict.CLEAR:
                break  # Passed, no more rounds needed

        # Build final report
        result = self.get_result(task.id)
        if result is None:
            return ReviewReport(
                task_id=task.id,
                verdict=ReviewVerdict.INCONCLUSIVE,
                passed=False,
                rounds_executed=rounds_executed,
                duration_sec=time.time() - start_time,
                error={'code': 'no_result', 'message': 'Review completed but no result extracted', 'recoverable': True},
            )

        passed = result.verdict == ReviewVerdict.CLEAR
        can_continue = not passed and rounds_executed < max_rounds
        latest_runs = self._db.list_runs(task.id)
        latest_run = latest_runs[-1] if latest_runs else None
        metrics: dict[str, object] = dict(latest_run.metrics) if latest_run and latest_run.metrics else {}

        # --- Finding dedup against history ---
        finding_statuses: list[FindingStatus] = []
        resolved_findings: list[StoredFinding] = []

        if is_incremental:
            new_stored = [
                StoredFinding(
                    severity=f.severity,
                    path=f.path or '',
                    line=f.line,
                    summary=f.summary,
                )
                for f in result.findings
            ]
            comparison = history.compare_findings(new_stored)
            finding_statuses = comparison['new'] + comparison['persistent']
            resolved_findings = [fs.finding for fs in comparison['resolved']]

        # --- Save review to history ---
        self._save_review_history(
            repo=repo,
            task=task,
            result=result,
            base=base,
            pr_url=pr_url,
            focus=inferred_focus,
            previous_sha=previous_sha,
            duration_sec=time.time() - start_time,
            metrics=metrics,
        )

        return ReviewReport(
            task_id=task.id,
            verdict=result.verdict,
            passed=passed,
            findings=result.findings,
            finding_count=result.finding_count,
            severity_counts=result.severity_counts,
            summary=result.summary,
            rounds_executed=rounds_executed,
            duration_sec=time.time() - start_time,
            can_continue=can_continue,
            finding_statuses=finding_statuses,
            resolved_findings=resolved_findings,
            is_incremental=is_incremental,
            previous_sha=previous_sha,
            inferred_focus=inferred_focus,
            metrics=metrics,
        )

    # ------------------------------------------------------------------
    # Inline review (direct LLM API — no subprocess)
    # ------------------------------------------------------------------

    def review_inline(
        self,
        repo_path: str,
        *,
        pr_url: str | None = None,
        base: str | None = None,
        review_focus: str = '',
        context: dict | None = None,
        timeout_sec: float = 300,
    ) -> ReviewReport:
        """One-shot review using direct LLM API call (no agent subprocess).

        This is the preferred mode for MCP integration — avoids nested
        subprocess issues (e.g., opencode-inside-opencode).

        Uses the same context compilation and findings parsing as the
        subprocess path, but calls the LLM HTTP API directly.
        """
        from app.adapters.direct_api_adapter import (
            DirectAPIAdapter,
            resolve_direct_api_config,
        )
        from app.core.config import load_repo_config

        start_time = time.time()
        repo = Path(repo_path).resolve()

        # Resolve API config
        amc_cfg = load_repo_config(str(repo))
        api_config = resolve_direct_api_config(amc_cfg)
        if not api_config.is_valid():
            return ReviewReport(
                task_id='',
                verdict=ReviewVerdict.FAILED,
                passed=False,
                error={
                    'code': 'config_error',
                    'message': 'Direct API config incomplete. Need base_url, api_key, and model. '
                               'Configure in ~/.codex/config.toml or ~/.config/amc/config.yaml (backend.direct_api section).',
                    'recoverable': False,
                },
            )

        # Infer review focus
        inferred_focus = review_focus
        if not review_focus and not pr_url:
            base_branch = base or self._detect_default_branch(str(repo))
            inferred_focus = self._infer_review_focus(str(repo), base_branch)

        # Create task (reuse existing logic for diff collection)
        try:
            task = self.create_task(
                repo_path,
                pr_url=pr_url,
                base=base,
                review_focus=inferred_focus,
                context=context,
            )
        except Exception as exc:
            return ReviewReport(
                task_id='',
                verdict=ReviewVerdict.FAILED,
                passed=False,
                error={'code': 'create_failed', 'message': str(exc), 'recoverable': False},
            )

        # Compile context and build prompt
        try:
            compile_started_at = time.perf_counter()
            compiled = self._contexts.compile_task(task, worktree_path=task.repo_path)
            compile_context_ms = int((time.perf_counter() - compile_started_at) * 1000)
            prompt_started_at = time.perf_counter()
            prompt = self._contexts.build_prompt(task, compiled, language=self.language)
            prompt_build_ms = int((time.perf_counter() - prompt_started_at) * 1000)
        except Exception as exc:
            return ReviewReport(
                task_id=task.id,
                verdict=ReviewVerdict.FAILED,
                passed=False,
                error={'code': 'compile_failed', 'message': str(exc), 'recoverable': False},
            )
        metrics: dict[str, object] = {
            'context_compile_ms': compile_context_ms,
            'prompt_build_ms': prompt_build_ms,
            'context_markdown_bytes': compiled.markdown_bytes,
            'context_json_bytes': compiled.json_bytes,
            'prompt_bytes': len(prompt.encode('utf-8')),
            'changed_file_count': len(task.review_paths),
            'patch_count': len(compiled.payload.get('review', {}).get('file_patches', []) or []),
            'candidate_file_count': len(compiled.payload.get('candidate_files', []) or []),
            'snippet_count': len(compiled.payload.get('related_snippets', []) or []),
            'top_level_entry_count': len(compiled.payload.get('repo', {}).get('top_level_entries', []) or []),
            'recent_commit_count': len(compiled.payload.get('repo', {}).get('recent_commits', []) or []),
        }
        self._engine.append_event(
            task_id=task.id,
            kind='review.metrics_collected',
            payload={'stage': 'prepare', **metrics},
        )

        # Call LLM API directly
        try:
            api_config.timeout = timeout_sec
            adapter = DirectAPIAdapter(api_config)
            llm_started_at = time.perf_counter()
            response_text = adapter.call_llm(prompt)
            metrics['llm_wall_time_ms'] = int((time.perf_counter() - llm_started_at) * 1000)
            metrics['response_text_bytes'] = len(response_text.encode('utf-8'))
        except Exception as exc:
            return ReviewReport(
                task_id=task.id,
                verdict=ReviewVerdict.FAILED,
                passed=False,
                duration_sec=time.time() - start_time,
                error={'code': 'api_error', 'message': str(exc), 'recoverable': True},
                metrics=metrics,
            )

        if not response_text.strip():
            return ReviewReport(
                task_id=task.id,
                verdict=ReviewVerdict.INCONCLUSIVE,
                passed=False,
                duration_sec=time.time() - start_time,
                error={'code': 'empty_response', 'message': 'LLM returned empty response', 'recoverable': True},
                metrics=metrics,
            )

        # Parse findings from raw text
        parse_started_at = time.perf_counter()
        result = ReviewResultService.parse_raw_text(response_text, language=self.language)
        metrics['parse_result_ms'] = int((time.perf_counter() - parse_started_at) * 1000)
        metrics['round_duration_ms'] = int((time.time() - start_time) * 1000)

        # Persist a Run and the latest result exactly like the subprocess path.
        # Without this the review completes but the Web UI, get_result() and
        # get_findings() all report zero findings.
        run = Run(
            task_id=task.id,
            backend=self.backend_name,
            round_index=len(self._db.list_runs(task.id)) + 1,
            status='completed',
            review_result=result,
            metrics=metrics,
            started_at=start_time,
            ended_at=time.time(),
            exit_code=0,
        )
        self._db.save_run(run)
        # Re-read: append_event() touches the row, so the local object may be stale.
        task = self._db.get_task(task.id) or task
        task.last_run_id = run.id
        task.latest_review_result = ReviewResultService.latest_result(self._db.list_runs(task.id))
        task.updated_at = time.time()
        self._db.save_task(task)

        # Store events for audit trail
        self._engine.append_event(
            task_id=task.id,
            run_id=run.id,
            kind='agent.text',
            payload={'text': response_text[:5000]},
        )
        self._engine.append_event(
            task_id=task.id,
            run_id=run.id,
            kind='review.result_extracted',
            payload={
                'verdict': result.verdict.value if hasattr(result.verdict, 'value') else str(result.verdict),
                'finding_count': result.finding_count,
                'mode': 'direct_api',
            },
        )
        self._engine.append_event(
            task_id=task.id,
            run_id=run.id,
            kind='review.metrics_collected',
            payload={'stage': 'finish', **metrics},
        )

        # Update task status
        self._engine.set_stage(task.id, status=TaskStatus.COMPLETED, stage='review_done')

        # Record the review so incremental mode works on this path too.
        self._save_review_history(
            repo=repo,
            task=task,
            result=result,
            base=base,
            pr_url=pr_url,
            focus=inferred_focus,
            previous_sha=None,
            duration_sec=time.time() - start_time,
            metrics=metrics,
        )

        passed = result.verdict == ReviewVerdict.CLEAR
        return ReviewReport(
            task_id=task.id,
            verdict=result.verdict,
            passed=passed,
            findings=result.findings,
            finding_count=result.finding_count,
            severity_counts=result.severity_counts,
            summary=result.summary,
            rounds_executed=1,
            duration_sec=time.time() - start_time,
            can_continue=False,
            inferred_focus=inferred_focus,
            metrics=metrics,
        )

    def _save_review_history(
        self,
        *,
        repo: Path,
        task: Task,
        result: ReviewResult,
        base: str | None,
        pr_url: str | None,
        focus: str,
        previous_sha: str | None,
        duration_sec: float,
        metrics: dict[str, object],
    ) -> None:
        """Append a review to the repo-local history store."""
        current_sha = self._get_head_sha(str(repo)) or ''
        record = ReviewRecord(
            review_id=str(uuid.uuid4())[:8],
            timestamp=time.time(),
            head_sha=current_sha,
            base_ref=base or self._detect_default_branch(str(repo)),
            mode='pr' if pr_url else 'local_diff',
            pr_url=pr_url,
            focus=focus,
            verdict=result.verdict.value if hasattr(result.verdict, 'value') else str(result.verdict),
            findings=[
                StoredFinding(
                    severity=f.severity,
                    path=f.path or '',
                    line=f.line,
                    summary=f.summary,
                )
                for f in result.findings
            ],
            finding_count=result.finding_count,
            duration_sec=duration_sec,
            changed_files=list(task.review_paths) if task.review_paths else [],
            commit_range=(
                f'{previous_sha[:7]}..{current_sha[:7]}' if previous_sha and current_sha else ''
            ),
            metadata={'metrics': metrics} if metrics else {},
        )
        ReviewHistoryStore(str(repo)).save_review(record)

    # ------------------------------------------------------------------
    # Local diff helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_default_branch(repo_path: str) -> str:
        """Detect main/master branch."""
        return resolve_base_branch(repo_path)

    @staticmethod
    def _get_local_changed_files(repo_path: str, base: str) -> list[str]:
        """Get files changed between base branch and HEAD, plus staged changes."""
        return get_local_changed_files(repo_path, base)

    @staticmethod
    def _get_local_diff(repo_path: str, base: str) -> str:
        """Get diff text: committed vs base + staged + unstaged."""
        return get_local_diff(repo_path, base)

    @staticmethod
    def _get_commit_messages(repo_path: str, base: str) -> list[str]:
        """Get commit messages since base branch."""
        return get_commit_messages(repo_path, base)

    @staticmethod
    def _get_head_sha(repo_path: str) -> str | None:
        """Get current HEAD commit SHA."""
        result = run_text(
            ['git', 'rev-parse', 'HEAD'],
            cwd=repo_path, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None

    @staticmethod
    def _infer_review_focus(repo_path: str, base: str) -> str:
        """Auto-infer review focus from recent commit messages and changed files.

        Looks for patterns like:
        - "fix: ..." → focus on correctness/regression
        - "feat: ..." → focus on design/completeness
        - "security" in messages → focus on security
        - "perf" in messages → focus on performance
        """
        # Get recent commit messages
        result = run_text(
            ['git', 'log', '--oneline', '-10', f'{base}..HEAD'],
            cwd=repo_path, timeout=10,
        )
        if result.returncode != 0:
            return ''

        messages = result.stdout.lower()

        # Pattern matching for focus areas
        focus_parts = []

        if 'security' in messages or 'auth' in messages or 'token' in messages:
            focus_parts.append('security')
        if 'perf' in messages or 'optim' in messages or 'speed' in messages:
            focus_parts.append('performance')
        if 'fix' in messages or 'bug' in messages or 'patch' in messages:
            focus_parts.append('correctness')
        if 'refactor' in messages:
            focus_parts.append('maintainability')
        if 'test' in messages:
            focus_parts.append('test coverage')

        # Also check what types of files changed
        file_result = run_text(
            ['git', 'diff', '--name-only', f'{base}..HEAD'],
            cwd=repo_path, timeout=10,
        )
        if file_result.returncode == 0:
            files = file_result.stdout.lower()
            if 'test' in files and 'test' not in ' '.join(focus_parts):
                focus_parts.append('test quality')
            if 'migration' in files or 'schema' in files:
                focus_parts.append('data integrity')
            if 'api' in files or 'route' in files:
                focus_parts.append('API design')

        if not focus_parts:
            return ''

        return ', '.join(focus_parts[:3])  # Max 3 focus areas
