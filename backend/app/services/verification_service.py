"""Decoupled cross-model Cold Falsification execution service.

Sibling to :class:`app.core.validation.ValidationService`:
- Shares the per-task exclusive reservation slot on ``ExecutionService`` so
  reviews, validations, verifications, and task deletions never race.
- Runs against the exact frozen snapshot worktree (never refreshes the local
  snapshot) so primary reviewer and verifier inspect byte-identical code.
- Executes strictly 1 bounded pass (no infinite agent debate loops).
- Never mutates ``TrackedFinding.status`` or auto-dismisses findings; persists
  independent evidence rows in SQLite ``finding_attestations``.
"""
from __future__ import annotations

import inspect
import logging
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import uuid4

from app.adapters.base import RunnerAdapter
from app.core.backend_detector import BackendDetector
from app.core.config import UNSET, SettingsResolver
from app.core.context_compiler import normalize_review_language
from app.core.db import Database
from app.core.mission_engine import MissionEngine
from app.core.models import (
    EventLevel,
    FindingAttestation,
    FindingStatus,
    RunSettings,
    Task,
    TaskStatus,
    TrackedFinding,
)
from app.core.proc import (
    mark_termination_requested,
    spawn_managed_process,
    terminate_process_tree,
    wait_for_process_exit,
)
from app.core.review_policy import prepare_static_review_env
from app.core.reviewer_selection import select_reviewer_backend
from app.core.verification_protocol import (
    build_falsification_prompt,
    parse_falsification_output,
    summarize_attestations,
)
from app.core.worktree import WorktreeManager
from app.services.artifact_store import ArtifactStore
from app.services.review_result_service import ReviewResultService

logger = logging.getLogger(__name__)

_OPEN_FINDING_STATUSES = {
    FindingStatus.NEW,
    FindingStatus.PERSISTENT,
    FindingStatus.UNVERIFIED,
    FindingStatus.LIKELY_FIXED,
}


@dataclass
class _ActiveVerification:
    id: str
    task_id: str
    backend: str
    model: str | None
    reasoning: str | None
    cross_model: bool
    selection_reason: str
    selection_note: str | None
    started_at: float = field(default_factory=time.time)
    process: subprocess.Popen[str] | None = None
    cancel_requested: bool = False
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    error: str | None = None


class VerificationService:
    """Runs bounded single-pass cross-model Cold Falsification on tracked findings."""

    def __init__(
        self,
        *,
        db: Database,
        mission: MissionEngine,
        worktrees: WorktreeManager,
        artifacts: ArtifactStore,
        adapters: dict[str, RunnerAdapter],
        execution=None,
        adapter_factory: Callable[..., RunnerAdapter] | None = None,
        detect_all_fn: Callable[[], list[Any]] | None = None,
    ):
        self.db = db
        self.mission = mission
        self.worktrees = worktrees
        self.artifacts = artifacts
        self.adapters = adapters
        self.execution = execution
        self.adapter_factory = adapter_factory
        self._detect_all = detect_all_fn or BackendDetector.detect_all

        self._lock = threading.Lock()
        self._active: dict[str, _ActiveVerification] = {}

    def is_verifying(self, task_id: str) -> bool:
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

    def _primary_backend_for_task(self, task: Task) -> str:
        runs = self.db.list_runs(task.id)
        for run in reversed(runs):
            if run.status in ('completed', 'finished') and run.backend:
                return run.backend
        if runs and runs[-1].backend:
            return runs[-1].backend
        return task.backend

    def _select_targets(
        self,
        task_id: str,
        finding_ids: list[str] | None,
    ) -> list[TrackedFinding]:
        all_findings = self.db.list_findings(task_id)
        if finding_ids:
            wanted = {fid.strip() for fid in finding_ids if fid and fid.strip()}
            selected = [f for f in all_findings if f.id in wanted]
            if not selected:
                raise ValueError('none of the requested finding_ids exist on this task')
            return selected
        open_findings = [f for f in all_findings if f.status in _OPEN_FINDING_STATUSES]
        if not open_findings:
            raise ValueError('no open findings to verify')
        return open_findings

    def start_verification(
        self,
        task_id: str,
        *,
        backend: str | None = None,
        model: str | None = None,
        reasoning: str | None = None,
        finding_ids: list[str] | None = None,
        language: str = 'en',
        timeout_sec: float = 180.0,
    ) -> dict[str, Any]:
        """Validate inputs, reserve the task slot, and start 1-pass Cold Falsification."""
        clean_lang = normalize_review_language(language)
        task = self.db.get_task(task_id)
        if task is None:
            raise KeyError(f'task not found: {task_id}')
        task = ReviewResultService.backfill_task(db=self.db, task=task)

        targets = self._select_targets(task_id, finding_ids)
        primary_backend = self._primary_backend_for_task(task)
        requested_backend = (backend or '').strip() or None
        if requested_backend and requested_backend.lower() == 'auto':
            requested_backend = None

        installed_names = [cap.name for cap in self._detect_all()]
        selection = select_reviewer_backend(
            requested=requested_backend,
            exclude=primary_backend if requested_backend is None else None,
            installed=installed_names,
        )
        if selection.backend is None:
            raise RuntimeError(
                selection.note
                or 'No supported coding agent CLI detected on PATH for verification.'
            )

        resolved_settings = SettingsResolver.resolve(
            task.repo_path,
            round_agent=selection.backend,
            round_model=(model.strip() or None) if model is not None else UNSET,
            round_reasoning=(reasoning.strip() or None) if reasoning is not None else UNSET,
            round_language=clean_lang,
        )
        if resolved_settings.agent not in self.adapters and self.adapter_factory is None:
            raise RuntimeError(f'verifier backend adapter not found: {resolved_settings.agent}')

        cross_model = resolved_settings.agent != primary_backend
        selection_note = selection.note
        if not cross_model and not selection_note:
            selection_note = (
                f'Verifier backend ({resolved_settings.agent}) matches primary reviewer '
                f'({primary_backend}); running cold-context verification without cross-vendor diversity.'
            )

        reserved = False
        if self.execution is not None and hasattr(self.execution, 'try_reserve_task'):
            if not self.execution.try_reserve_task(task_id, 'verification'):
                raise RuntimeError('task is currently busy; wait for the active operation to finish')
            reserved = True

        verification_id = uuid4().hex[:12]
        active = _ActiveVerification(
            id=verification_id,
            task_id=task_id,
            backend=resolved_settings.agent,
            model=resolved_settings.model,
            reasoning=resolved_settings.reasoning,
            cross_model=cross_model,
            selection_reason=selection.reason,
            selection_note=selection_note,
        )

        with self._lock:
            self._active[task_id] = active

        try:
            worker = threading.Thread(
                target=self._run_guarded,
                args=(active, task, targets, resolved_settings, primary_backend, float(timeout_sec), reserved),
                name=f'amc-verify-{task_id[:8]}',
                daemon=True,
            )
            worker.start()
        except BaseException:
            active.done.set()
            with self._lock:
                if self._active.get(task_id) is active:
                    self._active.pop(task_id, None)
            if reserved and self.execution is not None:
                self.execution.release_task(task_id, 'verification')
            raise

        return {
            'ok': True,
            'verification_id': verification_id,
            'task_id': task_id,
            'primary_backend': primary_backend,
            'verifier_backend': resolved_settings.agent,
            'model': resolved_settings.model,
            'reasoning': resolved_settings.reasoning,
            'cross_model': cross_model,
            'selection_reason': selection.reason,
            'selection_note': selection_note,
            'target_finding_ids': [f.id for f in targets],
            'target_count': len(targets),
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

    def run_verification_sync(
        self,
        task_id: str,
        *,
        backend: str | None = None,
        model: str | None = None,
        reasoning: str | None = None,
        finding_ids: list[str] | None = None,
        language: str = 'en',
        timeout_sec: float = 180.0,
    ) -> dict[str, Any]:
        """Run 1-pass Cold Falsification synchronously and return its structured summary."""
        started = self.start_verification(
            task_id,
            backend=backend,
            model=model,
            reasoning=reasoning,
            finding_ids=finding_ids,
            language=language,
            timeout_sec=timeout_sec,
        )
        with self._lock:
            active = self._active.get(task_id)
        if active is not None:
            active.done.wait(timeout=max(5.0, float(timeout_sec) + 15.0))
            if active.result is not None:
                return active.result
            if active.error:
                return {**started, 'ok': False, 'error': active.error}
        return {**started, 'ok': False, 'error': 'verification did not produce a result'}

    def abort_verification(self, task_id: str) -> bool:
        with self._lock:
            active = self._active.get(task_id)
            if active is None or active.done.is_set() or active.cancel_requested:
                return False
            active.cancel_requested = True
            proc = active.process

        if proc is not None and proc.poll() is None:
            mark_termination_requested(proc)
            terminate_process_tree(proc, grace_sec=2.0)
            wait_for_process_exit(proc, timeout=3.0)
        return True

    def _run_guarded(
        self,
        active: _ActiveVerification,
        task: Task,
        targets: list[TrackedFinding],
        settings: RunSettings,
        primary_backend: str,
        timeout_sec: float,
        reserved: bool,
    ) -> None:
        prev_status = task.status
        prev_stage = task.current_stage
        try:
            active.result = self._execute_verification(
                active, task, targets, settings, primary_backend, timeout_sec
            )
        except Exception as exc:
            active.error = str(exc)
            active.result = {
                'ok': False,
                'verification_id': active.id,
                'task_id': task.id,
                'verifier_backend': active.backend,
                'cross_model': active.cross_model,
                'error': str(exc),
            }
            logger.exception('Verification %s failed for task %s', active.id, task.id)
            try:
                self.mission.append_event(
                    task_id=task.id,
                    kind='verification.failed',
                    level=EventLevel.ERROR,
                    payload={
                        'verification_id': active.id,
                        'backend': active.backend,
                        'cross_model': active.cross_model,
                        'error': str(exc),
                    },
                )
            except Exception:
                pass
        finally:
            # Restore task to an interactive state without clobbering existing findings.
            try:
                latest = self.db.get_task(task.id)
                if latest is not None and latest.status == TaskStatus.VALIDATING:
                    restored_status = (
                        prev_status
                        if prev_status in (TaskStatus.WAITING_HUMAN, TaskStatus.COMPLETED, TaskStatus.CONTEXT_READY)
                        else TaskStatus.WAITING_HUMAN
                    )
                    restored_stage = (
                        'verification_completed'
                        if (active.result and active.result.get('ok'))
                        else ('verification_aborted' if active.cancel_requested else prev_stage)
                    )
                    self.mission.set_stage(task.id, status=restored_status, stage=restored_stage)
            except Exception:
                logger.exception('Failed to restore task %s stage after verification', task.id)
            active.done.set()
            if reserved and self.execution is not None:
                self.execution.release_task(task.id, 'verification')

    def _execute_verification(
        self,
        active: _ActiveVerification,
        task: Task,
        targets: list[TrackedFinding],
        settings: RunSettings,
        primary_backend: str,
        timeout_sec: float,
    ) -> dict[str, Any]:
        adapter = self._build_adapter(settings)
        if adapter is None:
            raise RuntimeError(f'verifier backend adapter not found: {settings.agent}')

        effective_model = getattr(adapter, 'model', settings.model)
        effective_reasoning = (
            getattr(adapter, 'reasoning', getattr(adapter, 'variant', None))
            if hasattr(adapter, 'reasoning') or hasattr(adapter, 'variant')
            else settings.reasoning
        )
        active.model = effective_model
        active.reasoning = effective_reasoning

        self.mission.set_stage(task.id, status=TaskStatus.VALIDATING, stage='verifying_findings')
        self.mission.append_event(
            task_id=task.id,
            kind='verification.started',
            payload={
                'verification_id': active.id,
                'primary_backend': primary_backend,
                'backend': settings.agent,
                'model': effective_model,
                'reasoning': effective_reasoning,
                'cross_model': active.cross_model,
                'selection_reason': active.selection_reason,
                'target_count': len(targets),
                'finding_ids': [f.id for f in targets],
            },
        )

        if active.cancel_requested:
            return self._record_aborted(active, task.id)

        # Reuse the exact frozen snapshot worktree without refreshing local_snapshot_sha.
        _branch, worktree_path, revision = self.worktrees.ensure_worktree(task)
        if active.cancel_requested:
            return self._record_aborted(active, task.id)

        prompt_text = build_falsification_prompt(targets, language=settings.language)
        self.artifacts.write_text(
            task.id,
            f'verification/prompt-{active.id}.md',
            prompt_text,
        )

        cmd = adapter.make_command(task=task, prompt=prompt_text)
        env = prepare_static_review_env(
            self.artifacts.task_dir(task.id) / 'policy-bin',
            base_env=None,
        )

        if active.cancel_requested:
            return self._record_aborted(active, task.id)

        proc = spawn_managed_process(
            cmd,
            cwd=worktree_path,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
        )
        with self._lock:
            active.process = proc
            cancelled_during_spawn = active.cancel_requested
        if cancelled_during_spawn:
            mark_termination_requested(proc)
            terminate_process_tree(proc, grace_sec=2.0)
            wait_for_process_exit(proc, timeout=3.0)
            with self._lock:
                active.process = None
            return self._record_aborted(active, task.id)

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []

        def _drain_stderr() -> None:
            if proc.stderr is None:
                return
            try:
                for line in proc.stderr:
                    stderr_chunks.append(line)
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
                active.process = None

        if active.cancel_requested:
            return self._record_aborted(active, task.id)
        if timed_out:
            raise TimeoutError(f'Verification timed out after {int(timeout_sec)}s')

        raw_combined = '\n'.join(text_parts).strip() or ''.join(stdout_chunks).strip()
        self.artifacts.write_text(
            task.id,
            f'verification/falsification-{active.id}.md',
            raw_combined,
        )

        if proc.returncode not in (0, None) and not raw_combined:
            err_tail = ''.join(stderr_chunks).strip()[-400:]
            raise RuntimeError(
                f'Verifier CLI `{settings.agent}` exited with code {proc.returncode}'
                + (f': {err_tail}' if err_tail else '')
            )

        ordered_ids = [f.id for f in targets]
        parsed, parse_mode = parse_falsification_output(raw_combined, ordered_finding_ids=ordered_ids)
        now = time.time()
        attestations = [
            FindingAttestation(
                task_id=task.id,
                finding_id=item.finding_id,
                kind='falsification',
                stance=item.stance,
                backend=settings.agent,
                model=effective_model,
                reasoning=effective_reasoning,
                cross_model=active.cross_model,
                review_revision=revision,
                rationale=item.rationale,
                evidence=item.evidence,
                trigger_path=item.trigger_path,
                created_at=now,
            )
            for item in parsed
        ]
        self.db.save_attestations(attestations)

        updated_findings = self.db.list_findings(task.id)
        summary = summarize_attestations(updated_findings)

        self.mission.append_event(
            task_id=task.id,
            kind='verification.completed',
            payload={
                'verification_id': active.id,
                'primary_backend': primary_backend,
                'backend': settings.agent,
                'model': effective_model,
                'reasoning': effective_reasoning,
                'cross_model': active.cross_model,
                'selection_reason': active.selection_reason,
                'review_revision': revision,
                'parse_mode': parse_mode,
                'summary': summary,
            },
        )
        return {
            'ok': True,
            'verification_id': active.id,
            'task_id': task.id,
            'primary_backend': primary_backend,
            'verifier_backend': settings.agent,
            'model': effective_model,
            'reasoning': effective_reasoning,
            'cross_model': active.cross_model,
            'selection_reason': active.selection_reason,
            'selection_note': active.selection_note,
            'review_revision': revision,
            'parse_mode': parse_mode,
            'summary': summary,
            'attestations': [a.model_dump() for a in attestations],
        }

    def _record_aborted(self, active: _ActiveVerification, task_id: str) -> dict[str, Any]:
        self.mission.append_event(
            task_id=task_id,
            kind='verification.aborted',
            level=EventLevel.WARNING,
            payload={
                'verification_id': active.id,
                'backend': active.backend,
            },
        )
        return {
            'ok': False,
            'aborted': True,
            'verification_id': active.id,
            'task_id': task_id,
            'verifier_backend': active.backend,
            'error': 'verification aborted',
        }
