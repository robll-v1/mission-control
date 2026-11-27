"""Same-snapshot Blind Dual-Review Jury + Targeted Disagreement Cold Falsification.

Sibling to :class:`app.services.verification_service.VerificationService`:
- Requires at least 2 distinct local coding agent CLIs (never fakes a 2-model
  jury with 1 CLI).
- Shares ``ExecutionService``'s per-task exclusive slot so reviews, validations,
  verifications, blind juries, and deletes never race.
- Runs Juror A and Juror B blindly in parallel against isolated detached read-only
  worktrees checked out at the exact same frozen snapshot revision.
- Merges findings across jurors without ever silently dropping 1/2 unique catches:
  * 2/2 Consensus -> 2 ``independent_review`` attestations (zero extra tokens spent).
  * 1/2 Unique -> 1 ``independent_review`` attestation, plus optional 1-pass Cold
    Falsification by the *other* juror (gracefully degrades to ``uncertain`` on
    falsification error/timeout so the blind jury result is never lost).
"""
from __future__ import annotations

import inspect
import logging
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from app.adapters.base import RunnerAdapter
from app.core.backend_detector import BackendDetector
from app.core.config import UNSET, SettingsResolver
from app.core.context_compiler import ContextCompiler, normalize_review_language
from app.core.db import Database
from app.core.finding_tracker import (
    compute_fingerprint,
    normalize_category,
    normalize_finding_path,
    normalize_title,
)
from app.core.mission_engine import MissionEngine
from app.core.models import (
    AttestationStance,
    EventLevel,
    FindingAttestation,
    FindingStatus,
    ReviewFinding,
    ReviewResult,
    ReviewVerdict,
    RunSettings,
    Task,
    TaskStatus,
    TrackedFinding,
)
from app.core.proc import (
    mark_termination_requested,
    run_text,
    spawn_managed_process,
    terminate_process_tree,
    wait_for_process_exit,
)
from app.core.review_policy import prepare_static_review_env
from app.core.reviewer_selection import REVIEWER_PRIORITY, normalize_agent_name
from app.core.verification_protocol import (
    build_falsification_prompt,
    parse_falsification_output,
    summarize_attestations,
)
from app.core.worktree import WorktreeManager
from app.services.artifact_store import ArtifactStore
from app.services.review_result_service import ReviewResultService

logger = logging.getLogger(__name__)

_SEVERITY_RANK = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}


@dataclass
class _JurorOutcome:
    backend: str
    model: str | None
    reasoning: str | None
    result: ReviewResult | None = None
    raw_text: str = ''
    error: str | None = None
    duration_sec: float = 0.0


@dataclass
class _ActiveJury:
    id: str
    task_id: str
    jurors: tuple[str, str]
    falsify_disagreements: bool
    started_at: float = field(default_factory=time.time)
    processes: list[subprocess.Popen[str]] = field(default_factory=list)
    cancel_requested: bool = False
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    error: str | None = None


def _findings_match(a: ReviewFinding, b: ReviewFinding) -> bool:
    """Determine whether two jurors independently reported the same underlying issue."""
    path_a = normalize_finding_path(a.path)
    path_b = normalize_finding_path(b.path)
    fp_a = compute_fingerprint(path_a, a.category, a.summary)
    fp_b = compute_fingerprint(path_b, b.category, b.summary)
    if fp_a == fp_b:
        return True

    title_a = normalize_title(a.summary)
    title_b = normalize_title(b.summary)
    sim = SequenceMatcher(None, title_a, title_b).ratio() if (title_a and title_b) else 0.0

    if path_a and path_b and path_a == path_b:
        line_close = (
            a.line is not None
            and b.line is not None
            and abs(int(a.line) - int(b.line)) <= 5
        )
        cat_a = normalize_category(a.category)
        cat_b = normalize_category(b.category)
        if line_close and (sim >= 0.35 or (cat_a and cat_a == cat_b) or a.severity.lower() == b.severity.lower()):
            return True
        if sim >= 0.65:
            return True
        return False

    return not path_a and not path_b and sim >= 0.75


def _higher_severity(sev_a: str, sev_b: str) -> str:
    return sev_a if _SEVERITY_RANK.get(sev_a.lower(), 9) <= _SEVERITY_RANK.get(sev_b.lower(), 9) else sev_b


def merge_jury_findings(
    juror_a: str,
    findings_a: list[ReviewFinding],
    juror_b: str,
    findings_b: list[ReviewFinding],
) -> list[tuple[ReviewFinding, list[str]]]:
    """Pure cross-juror merge returning ``(canonical_finding, [reporting_jurors])``."""
    merged: list[tuple[ReviewFinding, list[str]]] = []
    matched_b: set[int] = set()

    for fa in findings_a:
        best_idx: int | None = None
        for idx_b, fb in enumerate(findings_b):
            if idx_b in matched_b:
                continue
            if _findings_match(fa, fb):
                best_idx = idx_b
                break
        if best_idx is not None:
            matched_b.add(best_idx)
            fb = findings_b[best_idx]
            canonical = ReviewFinding(
                severity=_higher_severity(fa.severity, fb.severity),
                summary=fa.summary or fb.summary,
                path=fa.path or fb.path,
                line=fa.line if (fa.line and fa.line > 0) else fb.line,
                detail=fa.detail if len(fa.detail or '') >= len(fb.detail or '') else fb.detail,
                category=fa.category or fb.category,
            )
            merged.append((canonical, [juror_a, juror_b]))
        else:
            merged.append((fa, [juror_a]))

    for idx_b, fb in enumerate(findings_b):
        if idx_b not in matched_b:
            merged.append((fb, [juror_b]))

    return merged


class BlindJuryService:
    """Runs 2 blind independent reviewers on the same snapshot + targeted disagreement falsification."""

    def __init__(
        self,
        *,
        db: Database,
        mission: MissionEngine,
        worktrees: WorktreeManager,
        artifacts: ArtifactStore,
        contexts: ContextCompiler,
        adapters: dict[str, RunnerAdapter],
        execution=None,
        adapter_factory: Callable[..., RunnerAdapter] | None = None,
        detect_all_fn: Callable[[], list[Any]] | None = None,
    ):
        self.db = db
        self.mission = mission
        self.worktrees = worktrees
        self.artifacts = artifacts
        self.contexts = contexts
        self.adapters = adapters
        self.execution = execution
        self.adapter_factory = adapter_factory
        self._detect_all = detect_all_fn or BackendDetector.detect_all

        self._lock = threading.Lock()
        self._active: dict[str, _ActiveJury] = {}

    def is_running(self, task_id: str) -> bool:
        with self._lock:
            entry = self._active.get(task_id)
            return entry is not None and not entry.done.is_set()

    def _build_adapter(self, settings: RunSettings) -> RunnerAdapter | None:
        if self.adapter_factory is None:
            return self.adapters.get(settings.agent)
        sig = inspect.signature(self.adapter_factory)
        params = sig.parameters
        accepts_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        kwargs: dict[str, Any] = {'model': settings.model, 'variant': settings.reasoning}
        if accepts_varkw or 'reasoning' in params:
            kwargs['reasoning'] = settings.reasoning
        if accepts_varkw or '_resolved' in params:
            kwargs['_resolved'] = True
        return self.adapter_factory(settings.agent, **kwargs)

    def _resolve_jurors(self, requested: list[str] | None) -> tuple[str, str]:
        if requested:
            cleaned: list[str] = []
            for raw in requested:
                norm = normalize_agent_name(raw)
                if norm and norm not in cleaned:
                    cleaned.append(norm)
            if len(cleaned) < 2:
                raise ValueError(
                    'Blind jury requires at least 2 distinct reviewer backends '
                    '(e.g. ["codex", "claude-code"]).'
                )
            return cleaned[0], cleaned[1]

        installed = []
        seen: set[str] = set()
        for cap in self._detect_all():
            norm = normalize_agent_name(getattr(cap, 'name', None))
            if norm and norm not in seen:
                seen.add(norm)
                installed.append(norm)
        rank = {name: idx for idx, name in enumerate(REVIEWER_PRIORITY)}
        installed.sort(key=lambda name: (rank.get(name, len(REVIEWER_PRIORITY)), name))
        if len(installed) < 2:
            raise RuntimeError(
                f'Blind jury requires at least 2 installed local agent CLIs '
                f'(detected: {", ".join(installed) or "none"}). '
                'Install a second CLI (codex, claude, opencode, or gh copilot) to use blind dual-review.'
            )
        return installed[0], installed[1]

    def start_jury(
        self,
        task_id: str,
        *,
        backends: list[str] | None = None,
        falsify_disagreements: bool = True,
        language: str = 'en',
        timeout_sec: float = 180.0,
        parallel: bool = True,
    ) -> dict[str, Any]:
        """Validate 2 distinct jurors, reserve the task slot, and launch Blind Jury."""
        clean_lang = normalize_review_language(language)
        task = self.db.get_task(task_id)
        if task is None:
            raise KeyError(f'task not found: {task_id}')

        juror_a, juror_b = self._resolve_jurors(backends)
        for j in (juror_a, juror_b):
            if j not in self.adapters and self.adapter_factory is None:
                raise RuntimeError(f'juror backend adapter not found: {j}')

        reserved = False
        if self.execution is not None and hasattr(self.execution, 'try_reserve_task'):
            if not self.execution.try_reserve_task(task_id, 'blind_jury'):
                raise RuntimeError('task is currently busy; wait for the active operation to finish')
            reserved = True

        jury_id = uuid4().hex[:12]
        active = _ActiveJury(
            id=jury_id,
            task_id=task_id,
            jurors=(juror_a, juror_b),
            falsify_disagreements=bool(falsify_disagreements),
        )
        with self._lock:
            self._active[task_id] = active

        try:
            worker = threading.Thread(
                target=self._run_guarded,
                args=(active, task, clean_lang, float(timeout_sec), bool(parallel), reserved),
                name=f'amc-jury-{task_id[:8]}',
                daemon=True,
            )
            worker.start()
        except BaseException:
            active.done.set()
            with self._lock:
                if self._active.get(task_id) is active:
                    self._active.pop(task_id, None)
            if reserved and self.execution is not None:
                self.execution.release_task(task_id, 'blind_jury')
            raise

        return {
            'ok': True,
            'jury_id': jury_id,
            'task_id': task_id,
            'jurors': [juror_a, juror_b],
            'falsify_disagreements': bool(falsify_disagreements),
            'snapshot_sha': task.local_snapshot_sha or task.pr_head_sha,
        }

    def wait(self, task_id: str, timeout: float | None = None) -> dict[str, Any] | None:
        with self._lock:
            active = self._active.get(task_id)
        if active is None:
            return None
        active.done.wait(timeout=timeout)
        if not active.done.is_set():
            return None
        return active.result

    def run_jury_sync(
        self,
        task_id: str,
        *,
        backends: list[str] | None = None,
        falsify_disagreements: bool = True,
        language: str = 'en',
        timeout_sec: float = 180.0,
        parallel: bool = True,
    ) -> dict[str, Any]:
        started = self.start_jury(
            task_id,
            backends=backends,
            falsify_disagreements=falsify_disagreements,
            language=language,
            timeout_sec=timeout_sec,
            parallel=parallel,
        )
        with self._lock:
            active = self._active.get(task_id)
        if active is not None:
            active.done.wait(timeout=max(10.0, float(timeout_sec) * 2 + 20.0))
            if active.result is not None:
                return active.result
            if active.error:
                return {**started, 'ok': False, 'error': active.error}
        return {**started, 'ok': False, 'error': 'blind jury did not produce a result'}

    def abort_jury(self, task_id: str) -> bool:
        with self._lock:
            active = self._active.get(task_id)
            if active is None or active.done.is_set() or active.cancel_requested:
                return False
            active.cancel_requested = True
            procs = list(active.processes)

        for proc in procs:
            if proc.poll() is None:
                mark_termination_requested(proc)
                terminate_process_tree(proc, grace_sec=2.0)
                wait_for_process_exit(proc, timeout=3.0)
        return True

    def _run_guarded(
        self,
        active: _ActiveJury,
        task: Task,
        language: str,
        timeout_sec: float,
        parallel: bool,
        reserved: bool,
    ) -> None:
        prev_status = task.status
        prev_stage = task.current_stage
        try:
            active.result = self._execute_jury(active, task, language, timeout_sec, parallel)
        except Exception as exc:
            active.error = str(exc)
            active.result = {
                'ok': False,
                'jury_id': active.id,
                'task_id': task.id,
                'jurors': list(active.jurors),
                'error': str(exc),
            }
            logger.exception('Blind jury %s failed for task %s', active.id, task.id)
            try:
                self.mission.append_event(
                    task_id=task.id,
                    kind='jury.failed',
                    level=EventLevel.ERROR,
                    payload={'jury_id': active.id, 'jurors': list(active.jurors), 'error': str(exc)},
                )
            except Exception:
                pass
        finally:
            try:
                latest = self.db.get_task(task.id)
                if latest is not None and latest.status == TaskStatus.VALIDATING:
                    restored_status = (
                        prev_status
                        if prev_status in (TaskStatus.WAITING_HUMAN, TaskStatus.COMPLETED, TaskStatus.CONTEXT_READY)
                        else TaskStatus.WAITING_HUMAN
                    )
                    restored_stage = (
                        'jury_completed'
                        if (active.result and active.result.get('ok'))
                        else ('jury_aborted' if active.cancel_requested else prev_stage)
                    )
                    self.mission.set_stage(task.id, status=restored_status, stage=restored_stage)
            except Exception:
                logger.exception('Failed to restore task %s stage after blind jury', task.id)
            active.done.set()
            if reserved and self.execution is not None:
                self.execution.release_task(task.id, 'blind_jury')

    def _create_ephemeral_worktree(self, task: Task, revision: str, suffix: str) -> tuple[str, bool]:
        """Create an isolated detached worktree at ``revision`` for one juror."""
        wt_root = self.worktrees._worktrees_root_for_repo(task.repo_path)
        wt_root.mkdir(parents=True, exist_ok=True)
        path = wt_root / f'{task.id}-{suffix}'
        if path.exists():
            run_text(['git', '-C', task.repo_path, 'worktree', 'remove', '--force', str(path)], timeout=15)
            shutil.rmtree(path, ignore_errors=True)
        res = run_text(
            ['git', '-C', task.repo_path, 'worktree', 'add', '--detach', str(path), revision],
            timeout=30,
        )
        if res.returncode == 0 and path.is_dir():
            return str(path), True
        return '', False

    def _remove_ephemeral_worktree(self, repo_path: str, wt_path: str) -> None:
        if not wt_path:
            return
        try:
            run_text(['git', '-C', repo_path, 'worktree', 'remove', '--force', wt_path], timeout=15)
        except Exception:
            pass
        shutil.rmtree(wt_path, ignore_errors=True)

    def _run_subprocess_text(
        self,
        active: _ActiveJury,
        *,
        task: Task,
        adapter: RunnerAdapter,
        prompt_text: str,
        cwd: str,
        timeout_sec: float,
    ) -> tuple[str, int | None, bool, str]:
        """Execute an adapter command safely under ``active`` process tracking."""
        if active.cancel_requested:
            return '', None, False, 'aborted'

        cmd = adapter.make_command(task=task, prompt=prompt_text)
        env = prepare_static_review_env(
            self.artifacts.task_dir(task.id) / 'policy-bin',
            base_env=None,
        )
        proc = spawn_managed_process(
            cmd,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
        )
        with self._lock:
            active.processes.append(proc)
            cancelled_during_spawn = active.cancel_requested
        if cancelled_during_spawn:
            mark_termination_requested(proc)
            terminate_process_tree(proc, grace_sec=2.0)
            wait_for_process_exit(proc, timeout=3.0)
            return '', proc.returncode, False, 'aborted'

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []

        def _drain_stderr() -> None:
            if proc.stderr is None:
                return
            try:
                for ln in proc.stderr:
                    stderr_chunks.append(ln)
            except Exception:
                pass

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        timed_out = False

        def _watch_deadline() -> None:
            nonlocal timed_out
            deadline = time.monotonic() + max(1.0, timeout_sec)
            while proc.poll() is None and not active.cancel_requested:
                if time.monotonic() >= deadline:
                    timed_out = True
                    mark_termination_requested(proc)
                    terminate_process_tree(proc, grace_sec=2.0)
                    return
                time.sleep(0.1)

        watcher = threading.Thread(target=_watch_deadline, daemon=True)
        watcher.start()

        text_parts: list[str] = []
        try:
            if proc.stdout is not None:
                for raw_line in proc.stdout:
                    stdout_chunks.append(raw_line)
                    for ev in adapter.parse_stdout_line(raw_line):
                        if ev.kind == 'agent.text':
                            chunk = ev.payload.get('text')
                            if isinstance(chunk, str) and chunk:
                                text_parts.append(chunk)
                    if active.cancel_requested:
                        break
        finally:
            wait_for_process_exit(proc, timeout=5.0)
            stderr_thread.join(timeout=2.0)
            watcher.join(timeout=2.0)
            with self._lock:
                if proc in active.processes:
                    active.processes.remove(proc)

        combined = '\n'.join(text_parts).strip() or ''.join(stdout_chunks).strip()
        err_tail = ''.join(stderr_chunks).strip()[-400:]
        return combined, proc.returncode, timed_out, err_tail

    def _run_one_juror(
        self,
        active: _ActiveJury,
        *,
        task: Task,
        juror_backend: str,
        prompt_text: str,
        cwd: str,
        language: str,
        timeout_sec: float,
    ) -> _JurorOutcome:
        started = time.time()
        settings = SettingsResolver.resolve(
            task.repo_path,
            round_agent=juror_backend,
            round_model=UNSET,
            round_reasoning=UNSET,
            round_language=language,
        )
        adapter = self._build_adapter(settings)
        if adapter is None:
            return _JurorOutcome(
                backend=juror_backend,
                model=settings.model,
                reasoning=settings.reasoning,
                error=f'adapter not found: {juror_backend}',
                duration_sec=time.time() - started,
            )
        eff_model = getattr(adapter, 'model', settings.model)
        eff_reasoning = (
            getattr(adapter, 'reasoning', getattr(adapter, 'variant', None))
            if hasattr(adapter, 'reasoning') or hasattr(adapter, 'variant')
            else settings.reasoning
        )
        raw_text, returncode, timed_out, err_tail = self._run_subprocess_text(
            active,
            task=task,
            adapter=adapter,
            prompt_text=prompt_text,
            cwd=cwd,
            timeout_sec=timeout_sec,
        )
        if active.cancel_requested:
            return _JurorOutcome(
                backend=juror_backend,
                model=eff_model,
                reasoning=eff_reasoning,
                error='aborted',
                duration_sec=time.time() - started,
            )
        if timed_out:
            return _JurorOutcome(
                backend=juror_backend,
                model=eff_model,
                reasoning=eff_reasoning,
                error=f'juror {juror_backend} timed out after {int(timeout_sec)}s',
                duration_sec=time.time() - started,
            )
        self.artifacts.write_text(
            task.id,
            f'jury/juror-{juror_backend}-{active.id}.md',
            raw_text,
        )
        if returncode not in (0, None) and not raw_text:
            return _JurorOutcome(
                backend=juror_backend,
                model=eff_model,
                reasoning=eff_reasoning,
                error=f'juror {juror_backend} exited with code {returncode}' + (f': {err_tail}' if err_tail else ''),
                duration_sec=time.time() - started,
            )
        parsed = ReviewResultService.parse_raw_text(raw_text, language=language)
        return _JurorOutcome(
            backend=juror_backend,
            model=eff_model,
            reasoning=eff_reasoning,
            result=parsed,
            raw_text=raw_text,
            duration_sec=time.time() - started,
        )

    def _falsify_subset(
        self,
        active: _ActiveJury,
        *,
        task: Task,
        targets: list[TrackedFinding],
        verifier_backend: str,
        worktree_path: str,
        revision: str,
        language: str,
        timeout_sec: float,
    ) -> list[FindingAttestation]:
        """Run 1-pass Cold Falsification on a 1/2 disagreement subset with graceful degradation."""
        if not targets or active.cancel_requested:
            return []

        settings = SettingsResolver.resolve(
            task.repo_path,
            round_agent=verifier_backend,
            round_model=UNSET,
            round_reasoning=UNSET,
            round_language=language,
        )
        adapter = self._build_adapter(settings)
        now = time.time()
        if adapter is None:
            return self._degraded_uncertain(
                task.id, targets, verifier_backend, None, None, revision,
                f'Verifier adapter `{verifier_backend}` unavailable; original 1/2 finding preserved.',
                now,
            )

        eff_model = getattr(adapter, 'model', settings.model)
        eff_reasoning = (
            getattr(adapter, 'reasoning', getattr(adapter, 'variant', None))
            if hasattr(adapter, 'reasoning') or hasattr(adapter, 'variant')
            else settings.reasoning
        )
        prompt_text = build_falsification_prompt(targets, language=language)
        raw_text, returncode, timed_out, err_tail = self._run_subprocess_text(
            active,
            task=task,
            adapter=adapter,
            prompt_text=prompt_text,
            cwd=worktree_path,
            timeout_sec=timeout_sec,
        )
        if active.cancel_requested:
            return []
        if timed_out or (returncode not in (0, None) and not raw_text):
            reason = (
                f'Disagreement falsification by {verifier_backend} timed out after {int(timeout_sec)}s; original 1/2 finding preserved.'
                if timed_out
                else f'Disagreement falsification by {verifier_backend} failed (exit {returncode}); original 1/2 finding preserved.'
            )
            self.mission.append_event(
                task_id=task.id,
                kind='verification.failed',
                level=EventLevel.WARNING,
                payload={
                    'jury_id': active.id,
                    'backend': verifier_backend,
                    'error': reason,
                    'detail': err_tail,
                },
            )
            return self._degraded_uncertain(
                task.id, targets, verifier_backend, eff_model, eff_reasoning, revision, reason, now,
            )

        self.artifacts.write_text(
            task.id,
            f'jury/falsify-{verifier_backend}-{active.id}.md',
            raw_text,
        )
        parsed, _mode = parse_falsification_output(raw_text, ordered_finding_ids=[f.id for f in targets])
        return [
            FindingAttestation(
                task_id=task.id,
                finding_id=item.finding_id,
                kind='falsification',
                stance=item.stance,
                backend=verifier_backend,
                model=eff_model,
                reasoning=eff_reasoning,
                cross_model=True,
                review_revision=revision,
                rationale=item.rationale,
                evidence=item.evidence,
                trigger_path=item.trigger_path,
                created_at=now,
            )
            for item in parsed
        ]

    @staticmethod
    def _degraded_uncertain(
        task_id: str,
        targets: list[TrackedFinding],
        backend: str,
        model: str | None,
        reasoning: str | None,
        revision: str,
        reason: str,
        now: float,
    ) -> list[FindingAttestation]:
        return [
            FindingAttestation(
                task_id=task_id,
                finding_id=f.id,
                kind='falsification',
                stance=AttestationStance.UNCERTAIN,
                backend=backend,
                model=model,
                reasoning=reasoning,
                cross_model=True,
                review_revision=revision,
                rationale=reason,
                created_at=now,
            )
            for f in targets
        ]

    def _execute_jury(
        self,
        active: _ActiveJury,
        task: Task,
        language: str,
        timeout_sec: float,
        parallel: bool,
    ) -> dict[str, Any]:
        juror_a, juror_b = active.jurors
        self.mission.set_stage(task.id, status=TaskStatus.VALIDATING, stage='blind_jury_running')
        self.mission.append_event(
            task_id=task.id,
            kind='jury.started',
            payload={
                'jury_id': active.id,
                'jurors': [juror_a, juror_b],
                'falsify_disagreements': active.falsify_disagreements,
            },
        )
        if active.cancel_requested:
            return self._record_aborted(active, task.id)

        _branch, primary_wt, revision = self.worktrees.ensure_worktree(task)
        if active.cancel_requested:
            return self._record_aborted(active, task.id)

        compiled = self.contexts.compile_task(task, worktree_path=primary_wt)
        prompt_text = self.contexts.build_prompt(task, compiled, language=language)

        wt_a, created_a = self._create_ephemeral_worktree(task, revision, f'jury-a-{active.id[:6]}')
        wt_b, created_b = self._create_ephemeral_worktree(task, revision, f'jury-b-{active.id[:6]}')
        cwd_a = wt_a if created_a else primary_wt
        cwd_b = wt_b if created_b else primary_wt

        try:
            if parallel:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    fut_a = pool.submit(
                        self._run_one_juror,
                        active,
                        task=task,
                        juror_backend=juror_a,
                        prompt_text=prompt_text,
                        cwd=cwd_a,
                        language=language,
                        timeout_sec=timeout_sec,
                    )
                    fut_b = pool.submit(
                        self._run_one_juror,
                        active,
                        task=task,
                        juror_backend=juror_b,
                        prompt_text=prompt_text,
                        cwd=cwd_b,
                        language=language,
                        timeout_sec=timeout_sec,
                    )
                    outcome_a = fut_a.result()
                    outcome_b = fut_b.result()
            else:
                outcome_a = self._run_one_juror(
                    active, task=task, juror_backend=juror_a,
                    prompt_text=prompt_text, cwd=cwd_a, language=language, timeout_sec=timeout_sec,
                )
                outcome_b = self._run_one_juror(
                    active, task=task, juror_backend=juror_b,
                    prompt_text=prompt_text, cwd=cwd_b, language=language, timeout_sec=timeout_sec,
                )

            if active.cancel_requested:
                return self._record_aborted(active, task.id)

            if outcome_a.error and outcome_b.error:
                raise RuntimeError(
                    f'Both jurors failed: {juror_a} ({outcome_a.error}); {juror_b} ({outcome_b.error})'
                )

            findings_a = list(outcome_a.result.findings) if outcome_a.result else []
            findings_b = list(outcome_b.result.findings) if outcome_b.result else []
            merged = merge_jury_findings(juror_a, findings_a, juror_b, findings_b)

            # Persist merged findings into SQLite (reusing existing fingerprints when present)
            now = time.time()
            existing_by_fp = {f.fingerprint: f for f in self.db.list_findings(task.id)}
            tracked_to_save: list[TrackedFinding] = []
            indep_attestations: list[FindingAttestation] = []
            consensus_ids: list[str] = []
            unique_a_findings: list[TrackedFinding] = []
            unique_b_findings: list[TrackedFinding] = []

            outcome_map = {juror_a: outcome_a, juror_b: outcome_b}
            used_fps = set(existing_by_fp.keys())

            for idx, (rf, reporters) in enumerate(merged):
                fp = compute_fingerprint(rf.path, rf.category, rf.summary)
                existing = existing_by_fp.get(fp)
                if existing is not None:
                    tf = existing
                else:
                    if fp in used_fps:
                        fp = f'{fp[:12]}-{idx}'
                    used_fps.add(fp)
                    tf = TrackedFinding(
                        task_id=task.id,
                        fingerprint=fp,
                        category=rf.category,
                        severity=rf.severity,
                        path=normalize_finding_path(rf.path),
                        line=rf.line if (rf.line and rf.line > 0) else None,
                        title=rf.summary,
                        detail=rf.detail or rf.summary,
                        first_round=1,
                        last_round=1,
                        status=FindingStatus.NEW,
                        system_status=FindingStatus.NEW,
                        created_at=now,
                        updated_at=now,
                    )
                    tracked_to_save.append(tf)

                is_consensus = len(reporters) >= 2
                if is_consensus:
                    consensus_ids.append(tf.id)
                elif reporters == [juror_a]:
                    unique_a_findings.append(tf)
                elif reporters == [juror_b]:
                    unique_b_findings.append(tf)

                for rep in reporters:
                    out = outcome_map[rep]
                    indep_attestations.append(
                        FindingAttestation(
                            task_id=task.id,
                            finding_id=tf.id,
                            kind='independent_review',
                            stance=AttestationStance.CONFIRMED,
                            backend=rep,
                            model=out.model,
                            reasoning=out.reasoning,
                            cross_model=is_consensus,
                            review_revision=revision,
                            rationale=(
                                f'Independently reported by both {juror_a} and {juror_b} (2/2 consensus).'
                                if is_consensus
                                else f'Independently reported by {rep} (1/2 unique finding).'
                            ),
                            evidence=f'{tf.path}:{tf.line}' if (tf.path and tf.line) else (tf.path or ''),
                            created_at=now,
                        )
                    )

            if tracked_to_save:
                self.db.save_findings(tracked_to_save)
            if indep_attestations:
                self.db.save_attestations(indep_attestations)

            # Targeted Cold Falsification ONLY on 1/2 disagreements (0 extra tokens on 2/2 consensus!)
            falsification_attestations: list[FindingAttestation] = []
            if active.falsify_disagreements and not active.cancel_requested:
                if unique_a_findings and outcome_b.error is None:
                    falsification_attestations.extend(
                        self._falsify_subset(
                            active,
                            task=task,
                            targets=unique_a_findings,
                            verifier_backend=juror_b,
                            worktree_path=cwd_b,
                            revision=revision,
                            language=language,
                            timeout_sec=timeout_sec,
                        )
                    )
                if unique_b_findings and outcome_a.error is None and not active.cancel_requested:
                    falsification_attestations.extend(
                        self._falsify_subset(
                            active,
                            task=task,
                            targets=unique_b_findings,
                            verifier_backend=juror_a,
                            worktree_path=cwd_a,
                            revision=revision,
                            language=language,
                            timeout_sec=timeout_sec,
                        )
                    )
                if falsification_attestations:
                    self.db.save_attestations(falsification_attestations)

            if active.cancel_requested:
                return self._record_aborted(active, task.id)

            final_findings = self.db.list_findings(task.id)
            att_summary = summarize_attestations(final_findings)
            verdict = ReviewVerdict.CONCERNS if merged else ReviewVerdict.CLEAR
            summary_text = (
                f'Blind jury ({juror_a} + {juror_b}): {len(merged)} finding(s) '
                f'({len(consensus_ids)} consensus 2/2, {len(unique_a_findings)} unique to {juror_a}, '
                f'{len(unique_b_findings)} unique to {juror_b}).'
            )

            # Update latest_review_result on task so Web / API / MCP see unified verdict
            with self.db.locked():
                fresh = self.db.get_task(task.id)
                if fresh is not None:
                    sev_counts: dict[str, int] = {}
                    for rf, _reps in merged:
                        sev_counts[rf.severity] = sev_counts.get(rf.severity, 0) + 1
                    fresh.latest_review_result = ReviewResult(
                        verdict=verdict,
                        summary=summary_text,
                        findings=[rf for rf, _reps in merged],
                        severity_counts=sev_counts,
                        finding_count=len(merged),
                        parse_mode='json',
                    )
                    fresh.updated_at = time.time()
                    self.db.save_task(fresh)

            self.mission.append_event(
                task_id=task.id,
                kind='jury.completed',
                payload={
                    'jury_id': active.id,
                    'jurors': [juror_a, juror_b],
                    'review_revision': revision,
                    'total_findings': len(merged),
                    'consensus_count': len(consensus_ids),
                    'unique_by_juror': {
                        juror_a: len(unique_a_findings),
                        juror_b: len(unique_b_findings),
                    },
                    'falsification_runs': len(falsification_attestations),
                    'attestation_summary': att_summary,
                },
            )
            return {
                'ok': True,
                'jury_id': active.id,
                'task_id': task.id,
                'jurors': [juror_a, juror_b],
                'review_revision': revision,
                'verdict': verdict.value,
                'passed': verdict == ReviewVerdict.CLEAR,
                'summary': summary_text,
                'total_findings': len(merged),
                'consensus_count': len(consensus_ids),
                'consensus_finding_ids': consensus_ids,
                'unique_by_juror': {
                    juror_a: [f.id for f in unique_a_findings],
                    juror_b: [f.id for f in unique_b_findings],
                },
                'juror_errors': {
                    k: v
                    for k, v in ((juror_a, outcome_a.error), (juror_b, outcome_b.error))
                    if v
                },
                'attestation_summary': att_summary,
                'findings': [f.model_dump() for f in final_findings],
            }
        finally:
            if created_a:
                self._remove_ephemeral_worktree(task.repo_path, wt_a)
            if created_b:
                self._remove_ephemeral_worktree(task.repo_path, wt_b)

    def _record_aborted(self, active: _ActiveJury, task_id: str) -> dict[str, Any]:
        self.mission.append_event(
            task_id=task_id,
            kind='jury.aborted',
            level=EventLevel.WARNING,
            payload={'jury_id': active.id, 'jurors': list(active.jurors)},
        )
        return {
            'ok': False,
            'aborted': True,
            'jury_id': active.id,
            'task_id': task_id,
            'jurors': list(active.jurors),
            'error': 'blind jury aborted',
        }
