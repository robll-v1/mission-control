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
from app.core.config import UNSET, _UnsetType, resolve_backend_config
from app.core.db import Database
from app.core.execution import ExecutionService
from app.core.finding_tracker import FindingTracker, compute_fingerprint, normalize_finding_path
from app.core.github_ingest import fetch_pull_request, is_github_pr_url
from app.core.mission_engine import MissionEngine
from app.core.models import (
    Event,
    FindingStatus as TrackedFindingStatus,
    ReviewFinding,
    ReviewResult,
    ReviewVerdict,
    Run,
    Task,
    TaskStatus,
    TrackedFinding,
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
    unverified_findings: list[StoredFinding] = field(default_factory=list)
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
        backend: str | None = None,
        model: str | None = None,
        reasoning: str | None = None,
    ):
        self.language = language
        self._explicit_backend = backend
        self._explicit_model = model
        self._explicit_reasoning = reasoning
        initial = self._resolve_review_config('.')
        self.backend_name = str(initial['backend'])
        self.model = initial['model']
        self.reasoning = initial['variant']

        self._db = Database(db_path)
        self._engine = MissionEngine(self._db)
        # direct_api backend doesn't use subprocess adapters
        if self.backend_name == 'direct_api':
            self._adapters = {}
        else:
            self._adapters = {
                self.backend_name: get_adapter(
                    self.backend_name,
                    model=self.model,
                    variant=self.reasoning,
                    reasoning=self.reasoning,
                )
            }
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
            default_backend=self._explicit_backend,
            default_model=self._explicit_model,
            default_reasoning=self._explicit_reasoning,
            adapter_factory=self._make_adapter,
        )
        self._validation = ValidationService(
            db=self._db,
            mission=self._engine,
            artifacts=self._artifacts,
        )

    def _make_adapter(
        self,
        name: str,
        *,
        model: str | None = None,
        variant: str | None = None,
        reasoning: str | None = None,
        _resolved: bool = False,
    ):
        # ExecutionService already resolves constructor defaults, per-round
        # overrides, explicit None resets, and cross-agent isolation via
        # SettingsResolver (`_resolved=True`). Direct callers of
        # `adapter_factory(name, model=..., variant=...)` still get the
        # constructor-level explicit overrides for the configured backend.
        if _resolved:
            return get_adapter(name, model=model, variant=variant, reasoning=reasoning)
        matches_explicit = self._explicit_backend is None or self._explicit_backend == name
        eff_model = (self._explicit_model or model) if matches_explicit else model
        eff_reasoning = (
            (self._explicit_reasoning if self._explicit_reasoning is not None else (reasoning if reasoning is not None else variant))
            if matches_explicit
            else (reasoning if reasoning is not None else variant)
        )
        return get_adapter(name, model=eff_model, variant=eff_reasoning, reasoning=eff_reasoning)

    def _resolve_model(self, repo_path: str = '.') -> str | None:
        """Resolve the model from env var or merged config.

        Priority: ``$AMC_MODEL`` > ``backend.<backend>.model`` >
        ``backend.<default>.model`` > ``None`` (let the backend decide).

        This used to be declared ``@staticmethod`` while its body dereferenced
        ``self``; the resulting ``NameError`` was swallowed by a bare
        ``except Exception``, so configured models were silently ignored and
        only ``--model`` / ``$AMC_MODEL`` ever took effect.
        """
        return self._resolve_review_config(repo_path)['model']

    def _resolve_review_config(self, repo_path: str, *, backend: str | None = None) -> dict:
        return resolve_backend_config(
            repo_path,
            backend=backend if backend is not None else self._explicit_backend,
            model=self._explicit_model,
            reasoning=self._explicit_reasoning,
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

        resolved = self._resolve_review_config(str(repo))
        self.backend_name = str(resolved['backend'])
        self.model = resolved['model']

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
            model=self._explicit_model,
            reasoning=self._explicit_reasoning,
            language=self.language,
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
            generated_paths=self._generated_paths_for_repo(repo_path),
        )

        task = self._engine.create_task(
            title=local.title,
            repo_path=repo_path,
            description=local.description,
            source_type='local_diff',
            source_url=None,
            backend=self.backend_name,
            model=self._explicit_model,
            reasoning=self._explicit_reasoning,
            language=self.language,
            review_focus=review_focus,
            pr_base_ref=local.base_branch,
            review_paths=local.changed_files,
            local_head_sha=local.head_sha,
            local_snapshot_sha=local.snapshot_sha,
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
        backend: str | None | _UnsetType = UNSET,
        model: str | None | _UnsetType = UNSET,
        reasoning: str | None | _UnsetType = UNSET,
        language: str | None = None,
    ) -> Run:
        """Start a review round. Returns immediately; review runs in background thread."""
        return self._execution.start_task(
            task_id,
            prompt=prompt,
            review_note=review_note,
            backend=backend,
            model=model,
            reasoning=reasoning,
            language=language if language is not None else self.language,
        )

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
            if task.status in terminal_states and not self._execution.is_active(task_id):
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

    def get_tracked_findings(self, task_id: str) -> list[TrackedFinding]:
        """Get first-class cross-round findings stored in SQLite for a task."""
        task = self._db.get_task(task_id)
        if task is None:
            raise KeyError(f'task not found: {task_id}')
        ReviewResultService.backfill_task(db=self._db, task=task)
        return self._db.list_findings(task_id)

    def update_finding_status(self, task_id: str, finding_id: str, action: str) -> TrackedFinding:
        """Apply a user status action ('resolve', 'dismiss', or 'reopen') to a tracked finding."""
        task = self._db.get_task(task_id)
        if task is None:
            raise KeyError(f'task not found: {task_id}')
        ReviewResultService.backfill_task(db=self._db, task=task)
        return FindingTracker.set_user_status(
            db=self._db,
            task_id=task_id,
            finding_id=finding_id,
            action=action,
        )

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
        resolved = self._resolve_review_config(str(repo))
        self.backend_name = str(resolved['backend'])
        self.model = resolved['model']

        # --- Incremental: detect last reviewed SHA & prior open findings ---
        # SQLite is the sole source of truth; legacy `<repo>/.amc/reviews/` is
        # only read (never written) when SQLite has no prior local_diff review
        # for this repository.
        previous_sha: str | None = None
        previous_findings: list[StoredFinding] = []
        is_incremental = False
        if incremental and not pr_url:
            previous_sha, previous_findings = self._load_prior_local_state(repo)
            if previous_sha:
                is_incremental = True
                base = previous_sha

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
        latest_runs = self._db.list_runs(task.id)
        latest_run = latest_runs[-1] if latest_runs else None
        metrics: dict[str, object] = dict(latest_run.metrics) if latest_run and latest_run.metrics else {}

        # --- Finding dedup against prior local state (SQLite or read-only legacy import) ---
        finding_statuses: list[FindingStatus] = []
        resolved_findings: list[StoredFinding] = []
        unverified_findings: list[StoredFinding] = []

        if is_incremental and previous_findings:
            new_stored = [
                StoredFinding(
                    severity=f.severity,
                    path=f.path or '',
                    line=f.line,
                    summary=f.summary,
                )
                for f in result.findings
            ]
            comparison = self._compare_stored_findings(new_stored, previous_findings)
            finding_statuses = comparison['new'] + comparison['persistent'] + comparison['unverified']
            unverified_findings = [fs.finding for fs in comparison['unverified']]

        report_verdict = result.verdict
        report_passed = passed
        report_summary = result.summary
        report_severity_counts = dict(result.severity_counts)
        for finding in unverified_findings:
            report_severity_counts[finding.severity] = report_severity_counts.get(finding.severity, 0) + 1
        if unverified_findings:
            if result.verdict == ReviewVerdict.CLEAR:
                report_verdict = ReviewVerdict.CONCERNS
                report_passed = False
            report_summary = (
                f'{report_summary}\n\n{len(unverified_findings)} earlier finding(s) were not covered by this incremental review and remain unverified.'
            ).strip()
        can_continue = not report_passed and rounds_executed < max_rounds

        # Persist final tracked findings (including retained unverified findings)
        # into SQLite as the sole source of truth (never writing `.amc/reviews/`).
        self._persist_sdk_review_state(
            task=task,
            result=result,
            retained_findings=unverified_findings,
        )

        return ReviewReport(
            task_id=task.id,
            verdict=report_verdict,
            passed=report_passed,
            findings=result.findings,
            finding_count=result.finding_count + len(unverified_findings),
            severity_counts=report_severity_counts,
            summary=report_summary,
            rounds_executed=rounds_executed,
            duration_sec=time.time() - start_time,
            can_continue=can_continue,
            finding_statuses=finding_statuses,
            resolved_findings=resolved_findings,
            unverified_findings=unverified_findings,
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
            if task.source_type == 'local_diff':
                branch_name, worktree_path, _ = self._worktrees.ensure_worktree(task)
                task.branch_name = branch_name
                task.worktree_path = worktree_path
                self._db.save_task(task)
                try:
                    compiled = self._contexts.compile_task(task, worktree_path=worktree_path)
                finally:
                    self._worktrees.cleanup_worktree(task)
                    task.branch_name = None
                    task.worktree_path = None
                    self._db.save_task(task)
            else:
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
            review_revision=task.local_snapshot_sha or task.pr_head_sha,
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
        runs = self._db.list_runs(task.id)
        task.last_run_id = run.id
        task.latest_review_result = ReviewResultService.latest_result(runs)
        task.updated_at = time.time()
        self._db.save_task(task)
        FindingTracker.sync_round(db=self._db, task=task, run=run, all_runs=runs)

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
                'parse_mode': result.parse_mode,
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

    def _load_prior_local_state(self, repo: Path) -> tuple[str | None, list[StoredFinding]]:
        """Read prior local_diff SHA and open findings from SQLite, falling back
        read-only to legacy ``<repo>/.amc/reviews/`` when SQLite has no history
        for this repo yet. Never creates or writes ``.amc/reviews/``.
        """
        resolved_repo = str(repo.resolve())
        local_tasks = [
            t
            for t in self._db.list_tasks()
            if t.source_type == 'local_diff'
            and str(Path(t.repo_path).resolve()) == resolved_repo
            and t.latest_review_result is not None
        ]
        if local_tasks:
            local_tasks.sort(key=lambda t: (t.updated_at, t.created_at), reverse=True)
            prev_task = local_tasks[0]
            # Pick the review_revision of the latest COMPLETED (non-aborted,
            # CLEAR/CONCERNS) run instead of mutable `prev_task.local_snapshot_sha`
            # (which an aborted round's pre-prepare refresh can overwrite).
            prev_sha = prev_task.local_head_sha
            completed_runs = [
                r
                for r in self._db.list_runs(prev_task.id)
                if r.status == 'completed'
                and r.review_revision
                and (
                    r.review_result is None
                    or r.review_result.verdict in {ReviewVerdict.CLEAR, ReviewVerdict.CONCERNS}
                )
            ]
            if completed_runs:
                candidate_sha = completed_runs[-1].review_revision
                check = run_text(
                    ['git', 'cat-file', '-e', f'{candidate_sha}^{{commit}}'],
                    cwd=resolved_repo,
                    timeout=5,
                )
                if check.returncode == 0:
                    prev_sha = candidate_sha
            open_statuses = {
                TrackedFindingStatus.NEW,
                TrackedFindingStatus.PERSISTENT,
                TrackedFindingStatus.UNVERIFIED,
                TrackedFindingStatus.LIKELY_FIXED,
            }
            tracked = [
                f for f in self._db.list_findings(prev_task.id) if f.status in open_statuses
            ]
            stored = [
                StoredFinding(
                    severity=f.severity,
                    path=f.path or '',
                    line=f.line,
                    summary=f.title,
                    fingerprint=f.fingerprint,
                )
                for f in tracked
            ]
            return prev_sha, stored

        legacy_dir = repo / ReviewHistoryStore.STORE_DIR
        if (legacy_dir / ReviewHistoryStore.HISTORY_FILE).is_file() or (
            legacy_dir / ReviewHistoryStore.LATEST_FILE
        ).is_file():
            legacy = ReviewHistoryStore(str(repo))
            return (
                legacy.get_last_reviewed_sha(mode='local_diff'),
                legacy.get_previous_findings(mode='local_diff'),
            )
        return None, []

    @staticmethod
    def _compare_stored_findings(
        new_findings: list[StoredFinding],
        previous_findings: list[StoredFinding],
    ) -> dict[str, list[FindingStatus]]:
        """Compare findings in-memory without touching ``.amc/reviews/`` on disk."""
        store = object.__new__(ReviewHistoryStore)
        return store.compare_findings(new_findings, previous_findings=previous_findings)

    def _persist_sdk_review_state(
        self,
        *,
        task: Task,
        result: ReviewResult,
        retained_findings: list[StoredFinding],
    ) -> None:
        """Persist task's final ReviewResult and tracked findings into SQLite."""
        with self._db.locked():
            fresh = self._db.get_task(task.id) or task
            fresh.latest_review_result = result
            fresh.updated_at = time.time()
            self._db.save_task(fresh)

            existing = self._db.list_findings(task.id)
            used_fps = {item.fingerprint for item in existing}
            now = fresh.updated_at
            to_save: list[TrackedFinding] = []
            if not existing and result.findings:
                for idx, finding in enumerate(result.findings):
                    fp = compute_fingerprint(finding.path, finding.category, finding.summary)
                    if fp in used_fps:
                        fp = f'{fp[:12]}-{idx}'
                    used_fps.add(fp)
                    to_save.append(
                        TrackedFinding(
                            task_id=task.id,
                            fingerprint=fp,
                            category=finding.category,
                            severity=finding.severity,
                            path=normalize_finding_path(finding.path),
                            line=finding.line if (finding.line is not None and finding.line > 0) else None,
                            title=finding.summary,
                            detail=finding.detail or finding.summary,
                            first_round=1,
                            last_round=1,
                            status=TrackedFindingStatus.NEW,
                            system_status=TrackedFindingStatus.NEW,
                            user_status=None,
                            created_at=now,
                            updated_at=now,
                        )
                    )
            for idx, retained in enumerate(retained_findings):
                fp = compute_fingerprint(retained.path, '', retained.summary)
                if fp in used_fps:
                    continue
                used_fps.add(fp)
                to_save.append(
                    TrackedFinding(
                        task_id=task.id,
                        fingerprint=fp,
                        category='',
                        severity=retained.severity,
                        path=normalize_finding_path(retained.path),
                        line=retained.line if (retained.line is not None and retained.line > 0) else None,
                        title=retained.summary,
                        detail=retained.summary,
                        first_round=1,
                        last_round=1,
                        status=TrackedFindingStatus.UNVERIFIED,
                        system_status=TrackedFindingStatus.UNVERIFIED,
                        user_status=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
            if to_save:
                self._db.save_findings(to_save)

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

    def _generated_paths_for_repo(self, repo_path: str) -> list[str]:
        """Get list of generated/runtime paths (including DB backups) to exclude from snapshot."""
        repo = Path(repo_path).resolve()
        paths = [
            (self._artifacts.runtime_root / 'tasks').resolve(),
            self._worktrees._worktrees_root_for_repo(str(repo)),
            *(p.resolve() for p in self._db.generated_paths()),
        ]
        excluded: list[str] = []
        for path in paths:
            try:
                excluded.append(path.relative_to(repo).as_posix())
            except ValueError:
                continue
        return excluded

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
