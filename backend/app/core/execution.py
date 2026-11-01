from __future__ import annotations

import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from app.adapters.base import RunnerAdapter, resolve_executable
from app.core.config import load_repo_config, resolve_backend_config
from app.core.context_compiler import ContextCompiler
from app.core.db import Database
from app.core.github_ingest import fetch_pull_request
from app.core.mission_engine import MissionEngine
from app.core.models import EventLevel, Run, Task, TaskStatus
from app.core.review_policy import prepare_static_review_env
from app.core.worktree import WorktreeManager
from app.services.artifact_store import ArtifactStore
from app.services.local_review_service import capture_local_snapshot
from app.services.review_result_service import ReviewResultService

logger = logging.getLogger(__name__)


class IdleTimeoutError(RuntimeError):
    pass


class PreflightError(RuntimeError):
    pass


class RoundCancelledError(RuntimeError):
    """The round was aborted before its review agent started.

    Raised on the starting thread (preflight, prepare, handoff) and by the
    worker when the cancel lands before it launches the agent.
    """


class BackendUnavailableError(RuntimeError):
    """The requested backend has no adapter registered on this server.

    Distinct from "task not found" so the API can answer 409 instead of 404 and
    the user is not sent looking for a task that exists.
    """


class PullRequestRefreshError(RuntimeError):
    pass


@dataclass
class ActiveProcess:
    task_id: str
    run_id: str | None
    process: subprocess.Popen[str] | None
    worker: threading.Thread | None
    # Set by abort_task, in the same db-locked section that writes ABORTED.
    # Preflight and prepare run on the start caller's thread with no review
    # process to kill, so that thread (and later the worker) checks the flag
    # at every step instead.
    cancel_requested: bool = False
    # 'preflight' -> 'prepare' -> 'running'. Written under the db lock, so
    # abort_task reads it consistently; it labels the abort event and tells
    # a failed launch whether there is a worktree to clean up.
    phase: str = 'preflight'


class ExecutionService:
    def __init__(
        self,
        *,
        db: Database,
        mission: MissionEngine,
        adapters: dict[str, RunnerAdapter],
        worktrees: WorktreeManager,
        artifacts: ArtifactStore,
        contexts: ContextCompiler,
        adapter_factory=None,
    ):
        self.db = db
        self.mission = mission
        self.adapters = adapters
        self.worktrees = worktrees
        self.artifacts = artifacts
        self.contexts = contexts
        self.adapter_factory = adapter_factory
        self._lock = threading.Lock()
        self._active: dict[str, ActiveProcess] = {}

    def is_active(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._active

    def start_task(self, task_id: str, prompt: str | None = None, review_note: str | None = None, language: str = 'zh') -> Run:
        task = self.db.get_task(task_id)
        if task is None:
            raise KeyError(f'task not found: {task_id}')
        if task.backend not in self.adapters and self.adapter_factory is None:
            raise BackendUnavailableError(
                f'backend adapter not found: {task.backend}. '
                f'Available: {", ".join(sorted(self.adapters)) or "(none)"}'
            )
        with self._lock:
            if task_id in self._active:
                raise RuntimeError('a review round is already in progress for this PR')
            # Reserve the slot atomically with the check. All the prepare work
            # below used to happen before registration, so two concurrent
            # starts could both pass the check and launch rounds into the
            # same worktree.
            placeholder = ActiveProcess(task_id=task_id, run_id=None, process=None, worker=None)
            self._active[task_id] = placeholder
        try:
            return self._launch_round(task, prompt, review_note, language, placeholder)
        except BaseException as exc:
            try:
                # Settle the task while the slot is still held: once it is
                # released a restart may begin, and this cleanup must not
                # remove the restarted round's worktree or overwrite its stage.
                self._finish_failed_launch(task_id, placeholder, exc)
            except Exception:
                logger.exception('failed to settle task %s after an unsuccessful launch', task_id)
            finally:
                with self._lock:
                    # Pop only our own reservation: if the entry was replaced
                    # (a started round re-registers with its real process), it
                    # belongs to that round and must survive this unwind.
                    if self._active.get(task_id) is placeholder:
                        self._active.pop(task_id, None)
            raise

    @staticmethod
    def _raise_if_cancelled(placeholder: ActiveProcess) -> None:
        if placeholder.cancel_requested:
            raise RoundCancelledError('review round was cancelled before the review agent started')

    def _advance(self, placeholder: ActiveProcess, *, stage: str, phase: str) -> Task:
        """Enter the next prepare stage unless the round was cancelled.

        abort_task sets cancel_requested and writes ABORTED in one db-locked
        section, and this check-and-write holds the same lock. So either the
        abort comes after this write and replaces it, or this check sees it;
        a cancel can no longer be overwritten by the stage that follows it.
        """
        with self.db.locked():
            self._raise_if_cancelled(placeholder)
            placeholder.phase = phase
            return self.mission.set_stage(placeholder.task_id, status=TaskStatus.INGESTING, stage=stage)

    def _launch_round(
        self,
        task: Task,
        prompt: str | None,
        review_note: str | None,
        language: str,
        placeholder: ActiveProcess,
    ) -> Run:
        # Show the round straight away. Preflight alone may take up to 150s,
        # and the Web UI only offers to abort a round it can see.
        task = self._advance(placeholder, stage='preflight', phase='preflight')
        adapter = self.adapters.get(task.backend)
        if self.adapter_factory is not None:
            resolved = resolve_backend_config(task.repo_path, backend=task.backend)
            adapter = self.adapter_factory(task.backend, model=resolved['model'], variant=resolved['variant'])
        if adapter is None:
            raise BackendUnavailableError(f'backend adapter not found: {task.backend}')
        self._preflight_check(task, adapter=adapter)

        task = self._advance(placeholder, stage='prepare_review', phase='prepare')
        task = self._refresh_pull_request_snapshot(task)
        if task.source_type == 'local_diff':
            task = self._refresh_local_snapshot(task)
        # Not a correctness guard (the handoff below re-checks under the
        # lock), just no point creating a worktree for a cancelled round.
        self._raise_if_cancelled(placeholder)
        branch_name, worktree_path, revision = self.worktrees.ensure_worktree(task)
        task.branch_name = branch_name
        task.worktree_path = worktree_path
        compile_started_at = time.perf_counter()
        compiled = self.contexts.compile_task(task, worktree_path=worktree_path)
        compile_context_ms = int((time.perf_counter() - compile_started_at) * 1000)
        prompt_build_ms = 0
        prompt_text = prompt
        if prompt_text is None:
            prompt_started_at = time.perf_counter()
            prompt_text = self.contexts.build_prompt(task, compiled, review_note=review_note, language=language)
            prompt_build_ms = int((time.perf_counter() - prompt_started_at) * 1000)
        prepare_metrics = {
            'context_compile_ms': compile_context_ms,
            'prompt_build_ms': prompt_build_ms,
            'context_markdown_bytes': compiled.markdown_bytes,
            'context_json_bytes': compiled.json_bytes,
            'prompt_bytes': len(prompt_text.encode('utf-8')),
            'changed_file_count': len(task.review_paths),
            'patch_count': len(compiled.payload.get('review', {}).get('file_patches', []) or []),
            'candidate_file_count': len(compiled.payload.get('candidate_files', []) or []),
            'snippet_count': len(compiled.payload.get('related_snippets', []) or []),
            'top_level_entry_count': len(compiled.payload.get('repo', {}).get('top_level_entries', []) or []),
            'recent_commit_count': len(compiled.payload.get('repo', {}).get('recent_commits', []) or []),
        }
        self.mission.append_event(
            task_id=task.id,
            kind='task.review_context_compiled',
            payload={
                'markdown_path': compiled.markdown_path,
                'json_path': compiled.json_path,
                'candidate_files': len(compiled.payload.get('candidate_files', [])),
                'worktree_path': worktree_path,
                'review_revision': revision,
                'pr_head_sha': task.pr_head_sha,
                **prepare_metrics,
            },
        )

        idle_timeout_sec = self._idle_timeout_seconds(task.repo_path)
        # Handoff: the last cancel check, the Run, the task fields and RUNNING
        # form one db-locked section, so an abort lands either before it
        # (and no Run is created) or after it, when the Run is visible to
        # abort_task and the worker honours the flag before launching.
        with self.db.locked():
            self._raise_if_cancelled(placeholder)
            round_index = len(self.db.list_runs(task.id)) + 1
            run = Run(
                task_id=task.id,
                backend=task.backend,
                round_index=round_index,
                review_note=(review_note or '').strip(),
                review_revision=revision,
                metrics=prepare_metrics,
                started_at=time.time(),
                status='running',
            )
            self.db.save_run(run)
            placeholder.run_id = run.id
            placeholder.phase = 'running'
            self.mission.update_task(
                task.id, branch_name=branch_name, worktree_path=worktree_path, last_run_id=run.id,
            )
            self.mission.set_stage(task.id, status=TaskStatus.RUNNING, stage='review_in_progress')
            self.mission.append_event(
                task_id=task.id,
                run_id=run.id,
                kind='review.metrics_collected',
                payload={'stage': 'prepare', **prepare_metrics},
            )
        worker = threading.Thread(
            target=self._run_task,
            args=(
                task.id,
                run.id,
                round_index,
                prompt_text,
                idle_timeout_sec,
                language,
                adapter,
            ),
            daemon=True,
        )
        with self._lock:
            placeholder.worker = worker
        worker.start()
        return run

    def _finish_failed_launch(self, task_id: str, placeholder: ActiveProcess, exc: BaseException) -> None:
        """Leave a round that never reached its worker in a final state.

        A cancelled round keeps the ABORTED that abort_task wrote (set_stage
        will not leave it); anything else becomes FAILED with an error event
        naming the phase, because the task was already showing as INGESTING.
        """
        cancelled = placeholder.cancel_requested
        if placeholder.run_id is not None:
            # Only reachable if the handoff broke after creating the Run (a
            # database or thread-start error); never leave it 'running'.
            run = next((r for r in self.db.list_runs(task_id) if r.id == placeholder.run_id), None)
            if run is not None and run.status == 'running':
                run.status = 'aborted' if cancelled else 'failed'
                run.ended_at = time.time()
                self.db.save_run(run)
        if placeholder.phase != 'preflight':
            # Prepare may have refreshed or created this task's worktree.
            task = self.db.get_task(task_id)
            if task is not None:
                try:
                    self.worktrees.cleanup_worktree(task)
                except Exception:
                    logger.exception('failed to clean up the worktree of task %s', task_id)
                self.mission.update_task(task_id, branch_name=None, worktree_path=None)
        if not cancelled:
            self.mission.append_event(
                task_id=task_id,
                run_id=placeholder.run_id,
                kind='error',
                level=EventLevel.ERROR,
                payload={
                    'message': str(exc) or exc.__class__.__name__,
                    'type': exc.__class__.__name__,
                    'phase': placeholder.phase,
                },
            )
        self.mission.set_stage(
            task_id,
            status=TaskStatus.FAILED,
            stage='preflight_failed' if placeholder.phase == 'preflight' else 'review_prepare_failed',
        )

    def abort_task(self, task_id: str) -> bool:
        """Cancel the task's current round, wherever it is.

        Returns False only when there is nothing left to cancel: no round, a
        round whose cancel was already accepted, or a round that finished.
        """
        with self._lock:
            active = self._active.get(task_id)
            if active is None:
                return False
            with self.db.locked():
                if active.cancel_requested or self.db.get_task(task_id) is None:
                    return False
                if active.run_id is not None:
                    run = next((r for r in self.db.list_runs(task_id) if r.id == active.run_id), None)
                    if run is None or run.status != 'running':
                        # Finalisation takes this lock too, so a round that
                        # already completed cannot accept a late abort while
                        # its worktree is being cleaned up.
                        return False
                # The flag and ABORTED are published together. Every stage
                # change on the starting thread checks the flag under this
                # lock, and the worker checks it before launching the agent.
                active.cancel_requested = True
                self.mission.set_stage(task_id, status=TaskStatus.ABORTED, stage='aborted')
                self.mission.append_event(
                    task_id=task_id,
                    run_id=active.run_id,
                    kind='control.accepted',
                    payload={'action': 'abort', 'phase': active.phase},
                )
            # The preflight probe or the review agent. With neither running
            # yet, whichever thread is about to spawn one sees the flag first.
            process = active.process
        if process is not None:
            self._terminate_process(process)
        return True

    def _persist_round_result(self, task_id: str, run: Run, language: str) -> bool:
        """Commit the result and task state together with respect to abort."""
        with self.db.locked():
            task = self.db.get_task(task_id)
            was_aborted = task is not None and task.status == TaskStatus.ABORTED
            if was_aborted:
                run.status = 'aborted'
                # Partial agent output must never certify a cancelled review.
                run.review_result = ReviewResultService.extract_result(
                    events=self.db.list_events(task_id), run=run, language=language
                )
            self.db.save_run(run)
            if task is not None:
                task.latest_review_result = ReviewResultService.latest_result(self.db.list_runs(task_id))
                self.db.save_task(task)
                if not was_aborted:
                    completed = run.status == 'completed'
                    self.mission.set_stage(
                        task_id,
                        status=TaskStatus.WAITING_HUMAN if completed else TaskStatus.FAILED,
                        stage='awaiting_next_round' if completed else 'review_failed',
                    )
            return was_aborted

    def cleanup_task_worktree(self, task_id: str, *, reason: str = 'manual') -> dict[str, object]:
        task = self.db.get_task(task_id)
        if task is None:
            raise KeyError(f'task not found: {task_id}')
        with self._lock:
            if task_id in self._active:
                raise RuntimeError('cannot cleanup worktree while a review round is running')
        runs = self.db.list_runs(task_id)
        latest_run = runs[-1] if runs else None
        return self._cleanup_task_worktree(
            task,
            run_id=latest_run.id if latest_run is not None else None,
            round_index=latest_run.round_index if latest_run is not None else None,
            reason=reason,
        )

    def _run_task(
        self,
        task_id: str,
        run_id: str,
        round_index: int,
        prompt: str,
        idle_timeout_sec: int,
        language: str = 'en',
        adapter=None,
    ) -> None:
        task = self.db.get_task(task_id)
        run = next((item for item in self.db.list_runs(task_id) if item.id == run_id), None)
        if task is None or run is None:
            return

        proc: subprocess.Popen[str] | None = None
        try:
            # An abort accepted during the handoff found no process to kill;
            # it is honoured here, before any agent exists.
            with self._lock:
                slot = self._active.get(task.id)
                if slot is not None:
                    self._raise_if_cancelled(slot)
            adapter = adapter or self.adapters[task.backend]
            cmd = adapter.make_command(task=task, prompt=prompt)
            llm_started_at = time.perf_counter()
            agent_event_count = 0
            agent_text_bytes = 0
            self.mission.append_event(
                task_id=task.id,
                run_id=run.id,
                kind='review.round_started',
                payload={
                    'entrypoint': cmd[:4],
                    'backend': task.backend,
                    'worktree_path': task.worktree_path,
                    'idle_timeout_sec': idle_timeout_sec,
                    'round_index': round_index,
                    'review_revision': run.review_revision,
                    'pr_head_sha': task.pr_head_sha,
                },
            )
            proc = subprocess.Popen(
                cmd,
                cwd=task.worktree_path or task.repo_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1,
                start_new_session=(sys.platform != 'win32'),
                **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {}),
                env=prepare_static_review_env(self.worktrees.runtime_root),
            )
            with self._lock:
                slot = self._active.get(task.id)
                if slot is None:
                    slot = ActiveProcess(task_id=task.id, run_id=run.id, process=None, worker=None, phase='running')
                    self._active[task.id] = slot
                # Register on the same slot so a cancel accepted while the
                # agent was spawning is not lost with a replaced entry.
                slot.process = proc
                slot.worker = threading.current_thread()
                cancelled_while_spawning = slot.cancel_requested
            if cancelled_while_spawning:
                # abort_task had no process to kill. Stopping it here lets the
                # normal exit path below finalise the round as aborted.
                self._terminate_process(proc)

            line_queue: queue.Queue[tuple[str, str]] = queue.Queue()
            last_activity_at = time.time()

            def pump(stream_name: str, stream):
                try:
                    for line in iter(stream.readline, ''):
                        line_queue.put((stream_name, line))
                finally:
                    stream.close()

            stdout_thread = threading.Thread(target=pump, args=('stdout', proc.stdout), daemon=True)
            stderr_thread = threading.Thread(target=pump, args=('stderr', proc.stderr), daemon=True)
            stdout_thread.start()
            stderr_thread.start()

            while stdout_thread.is_alive() or stderr_thread.is_alive() or not line_queue.empty():
                try:
                    stream_name, line = line_queue.get(timeout=0.5)
                except queue.Empty:
                    if proc.poll() is not None and line_queue.empty() and not stdout_thread.is_alive() and not stderr_thread.is_alive():
                        break
                    if time.time() - last_activity_at >= idle_timeout_sec:
                        self.mission.append_event(
                            task_id=task.id,
                            run_id=run.id,
                            kind='warning',
                            level=EventLevel.WARNING,
                            payload={
                                'message': f'No new output for {idle_timeout_sec}s; terminating stalled review round',
                                'reason': 'idle_timeout',
                                'round_index': round_index,
                            },
                        )
                        self._terminate_process(proc)
                        raise IdleTimeoutError(f'No new output for {idle_timeout_sec}s')
                    continue

                last_activity_at = time.time()
                events = adapter.parse_stdout_line(line) if stream_name == 'stdout' else adapter.parse_stderr_line(line)
                for event in events:
                    agent_event_count += 1
                    text = event.payload.get('text')
                    if event.kind == 'agent.text' and isinstance(text, str):
                        agent_text_bytes += len(text.encode('utf-8'))
                    if 'session_id' in event.payload and not run.backend_session_id:
                        run.backend_session_id = str(event.payload['session_id'])
                        self.db.save_run(run)
                    self.mission.append_event(
                        task_id=task.id,
                        run_id=run.id,
                        kind=event.kind,
                        level=EventLevel(event.level),
                        payload=event.payload,
                    )

            exit_code = proc.wait(timeout=5)
            run.ended_at = time.time()
            run.exit_code = exit_code
            run.status = 'completed' if exit_code == 0 else 'failed'
            parse_started_at = time.perf_counter()
            run.review_result = ReviewResultService.extract_result(
                events=self.db.list_events(task_id), run=run, language=language
            )
            parse_result_ms = int((time.perf_counter() - parse_started_at) * 1000)
            run.metrics.update({
                'llm_wall_time_ms': int((time.perf_counter() - llm_started_at) * 1000),
                'parse_result_ms': parse_result_ms,
                'agent_event_count': agent_event_count,
                'agent_text_bytes': agent_text_bytes,
                'round_duration_ms': int((run.ended_at - run.started_at) * 1000),
            })
            was_aborted = self._persist_round_result(task.id, run, language)

            if run.review_result is not None:
                self.mission.append_event(
                    task_id=task.id,
                    run_id=run.id,
                    kind='review.result_extracted',
                    level=EventLevel.WARNING if run.review_result.verdict == 'concerns' else EventLevel.INFO,
                    payload={
                        'round_index': round_index,
                        'verdict': run.review_result.verdict,
                        'finding_count': run.review_result.finding_count,
                        'severity_counts': run.review_result.severity_counts,
                        'summary': run.review_result.summary,
                    },
                )
            self.mission.append_event(
                task_id=task.id,
                run_id=run.id,
                kind='review.metrics_collected',
                payload={'stage': 'finish', **run.metrics},
            )

            self.mission.append_event(
                task_id=task.id,
                run_id=run.id,
                kind='review.round_finished',
                level=EventLevel.INFO if exit_code == 0 else EventLevel.ERROR,
                payload={
                    'exit_code': exit_code,
                    'session_id': run.backend_session_id,
                    'round_index': round_index,
                    **({'reason': 'aborted'} if was_aborted else {}),
                    'verdict': run.review_result.verdict if run.review_result is not None else None,
                    'finding_count': run.review_result.finding_count if run.review_result is not None else 0,
                    'severity_counts': run.review_result.severity_counts if run.review_result is not None else {},
                },
            )
        except Exception as exc:
            if proc is not None and proc.poll() is None:
                self._terminate_process(proc)
            run.ended_at = time.time()
            run.exit_code = -2 if isinstance(exc, IdleTimeoutError) else -1
            run.status = 'failed'
            parse_started_at = time.perf_counter()
            run.review_result = ReviewResultService.extract_result(
                events=self.db.list_events(task_id), run=run, language=language
            )
            parse_result_ms = int((time.perf_counter() - parse_started_at) * 1000)
            llm_elapsed_ms = int((time.perf_counter() - llm_started_at) * 1000) if 'llm_started_at' in locals() else 0
            run.metrics.update({
                'llm_wall_time_ms': llm_elapsed_ms,
                'parse_result_ms': parse_result_ms,
                'agent_event_count': agent_event_count if 'agent_event_count' in locals() else 0,
                'agent_text_bytes': agent_text_bytes if 'agent_text_bytes' in locals() else 0,
                'round_duration_ms': int((run.ended_at - run.started_at) * 1000),
            })
            was_aborted = self._persist_round_result(task.id, run, language)
            if not was_aborted:
                # A cancelled round is reported by control.accepted and the
                # 'aborted' round_finished below; it is not an error.
                self.mission.append_event(
                    task_id=task.id,
                    run_id=run.id,
                    kind='error',
                    level=EventLevel.ERROR,
                    payload={'message': str(exc), 'type': exc.__class__.__name__, 'round_index': round_index},
                )
            self.mission.append_event(
                task_id=task.id,
                run_id=run.id,
                kind='review.metrics_collected',
                payload={'stage': 'finish', **run.metrics},
            )
            self.mission.append_event(
                task_id=task.id,
                run_id=run.id,
                kind='review.round_finished',
                level=EventLevel.ERROR,
                payload={
                    'exit_code': run.exit_code,
                    'session_id': run.backend_session_id,
                    'reason': 'aborted' if was_aborted else ('idle_timeout' if isinstance(exc, IdleTimeoutError) else 'exception'),
                    'round_index': round_index,
                    'verdict': run.review_result.verdict if run.review_result is not None else 'failed',
                    'finding_count': run.review_result.finding_count if run.review_result is not None else 0,
                },
            )
        finally:
            try:
                if task is not None and self._should_auto_cleanup_worktree(task.repo_path):
                    self._cleanup_task_worktree(
                        task,
                        run_id=run.id if run is not None else None,
                        round_index=round_index,
                        reason='round_finished',
                    )
            finally:
                with self._lock:
                    self._active.pop(task.id, None)

    @staticmethod
    def _terminate_process(proc: subprocess.Popen[str]) -> None:
        if proc.returncode is not None:
            # Already reaped (abort and the probe wait can both stop the
            # probe). Its pid, and so its process group id, may now belong
            # to an unrelated process.
            return
        if sys.platform == 'win32':
            # Windows: no process groups via os.killpg
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except OSError:
                    pass
            return

        # Unix: terminate the whole process group
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except Exception:
            try:
                proc.terminate()
            except OSError:
                return
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except OSError:
                    pass

    def _preflight_check(
        self,
        task: Task,
        *,
        adapter: RunnerAdapter | None = None,
        # 150s, not 60s: the first call after a backend's background service
        # cold-starts (observed with opencode) can exceed 60s.
        probe_timeout_sec: int = 150,
    ) -> None:
        adapter = adapter or self.adapters.get(task.backend)
        if adapter is None:
            raise BackendUnavailableError(
                f'backend adapter not configured: {task.backend}. '
                f'Available: {", ".join(sorted(self.adapters)) or "(none)"}'
            )

        backend_name = adapter.name
        # The backend key and the program name differ for most adapters
        # (claude-code -> claude, copilot -> gh); probing the key would reject
        # a CLI that is installed and working.
        executable = adapter.executable_name()
        # resolve_executable() is what make_command() uses to launch, and it
        # honours PATHEXT so npm/gh/codex .cmd shims resolve on Windows. It
        # returns the bare name unchanged when nothing was found.
        if resolve_executable(executable) == executable and shutil.which(executable) is None:
            raise PreflightError(
                f'{executable} (backend "{backend_name}") is not installed or not in PATH. '
                f'Install it before starting a review.'
            )

        # The reserved slot (if this call is part of start_task) carries the
        # cancellation flag and receives the probe process. Standalone calls
        # (tests, manual probes) simply run uncancellable.
        with self._lock:
            active = self._active.get(task.id)

        probe = adapter.probe_command()
        if probe is not None:
            if active is not None:
                self._raise_if_cancelled(active)
            try:
                # Popen (not subprocess.run) so the probe is visible in the
                # active slot and an abort during the up-to-150s wait can
                # terminate exactly this subprocess. Same process-group
                # treatment as the review agent itself.
                probe_proc = subprocess.Popen(
                    probe,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    cwd=task.repo_path,
                    start_new_session=(sys.platform != 'win32'),
                    **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {}),
                )
            except FileNotFoundError:
                raise PreflightError(f'{executable} executable not found.')
            with self._lock:
                if active is not None:
                    if active.cancel_requested:
                        # Abort landed while we were spawning: kill the probe
                        # ourselves; abort_task saw process=None.
                        self._terminate_process(probe_proc)
                        raise RoundCancelledError('review round was cancelled before the preflight probe started')
                    active.process = probe_proc
            try:
                stdout_text, stderr_text = self._await_probe(
                    probe_proc,
                    active=active,
                    probe_timeout_sec=probe_timeout_sec,
                    executable=executable,
                )
            except BaseException:
                # Covers Ctrl+C in `amc review`: the probe runs in its own
                # session, so the terminal's SIGINT never reached it.
                self._terminate_process(probe_proc)
                raise
            finally:
                with self._lock:
                    if active is not None and active.process is probe_proc:
                        active.process = None
            if active is not None and active.cancel_requested:
                raise RoundCancelledError('review round was cancelled while the preflight probe was finishing')
            if probe_proc.returncode != 0:
                stderr_preview = (stderr_text or '').strip()[:300]
                raise PreflightError(
                    f'{executable} preflight test failed (exit {probe_proc.returncode}). '
                    f'Check your API key and configuration. stderr: {stderr_preview}'
                )

        self.mission.append_event(
            task_id=task.id,
            kind='task.preflight_passed',
            payload={'backend': backend_name, 'executable': executable},
        )

    def _await_probe(
        self,
        probe_proc: subprocess.Popen[str],
        *,
        active: ActiveProcess | None,
        probe_timeout_sec: int,
        executable: str,
    ) -> tuple[str, str]:
        """Wait for the probe while watching for cancellation and timeout.

        The poll interval bounds both the abort latency and the timeout
        overshoot. On cancellation or timeout the probe process is terminated
        and reaped before raising, so nothing leaks.
        """
        deadline = time.monotonic() + probe_timeout_sec
        while True:
            try:
                stdout_text, stderr_text = probe_proc.communicate(timeout=0.25)
                return stdout_text or '', stderr_text or ''
            except subprocess.TimeoutExpired:
                cancelled = active is not None and active.cancel_requested
                if cancelled or time.monotonic() >= deadline:
                    self._terminate_process(probe_proc)
                    try:
                        probe_proc.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    if cancelled:
                        raise RoundCancelledError(
                            'review round was cancelled while the preflight probe was running'
                        ) from None
                    raise PreflightError(
                        f'{executable} preflight test timed out after {probe_timeout_sec}s. '
                        f'The LLM backend may be unreachable or still cold-starting.'
                    ) from None

    @staticmethod
    def _idle_timeout_seconds(repo_path: str) -> int:
        cfg = load_repo_config(repo_path)
        execution_cfg = cfg.get('execution', {}) if isinstance(cfg, dict) else {}
        try:
            timeout = int(execution_cfg.get('idle_timeout_sec', 180))
        except (TypeError, ValueError):
            timeout = 180
        return max(timeout, 30)

    @staticmethod
    def _should_auto_cleanup_worktree(repo_path: str) -> bool:
        cfg = load_repo_config(repo_path)
        worktree_cfg = cfg.get('worktree', {}) if isinstance(cfg, dict) else {}
        return bool(worktree_cfg.get('auto_cleanup', True))

    def _cleanup_task_worktree(
        self,
        task: Task,
        *,
        run_id: str | None,
        round_index: int | None,
        reason: str,
    ) -> dict[str, object]:
        result = self.worktrees.cleanup_worktree(task)
        fresh_task = self.db.get_task(task.id) or task
        if not result.get('exists_after'):
            fresh_task.worktree_path = None
        if result.get('branch_removed') and (fresh_task.branch_name or '') == f'review/{task.id}':
            fresh_task.branch_name = None
        fresh_task.updated_at = time.time()
        self.db.save_task(fresh_task)

        self.mission.append_event(
            task_id=task.id,
            run_id=run_id,
            kind='task.worktree_cleaned' if not result['errors'] else 'task.worktree_cleanup_failed',
            level=EventLevel.INFO if not result['errors'] else EventLevel.WARNING,
            payload={
                'reason': reason,
                'round_index': round_index,
                'worktree_path': result['worktree_path'],
                'removed': result['removed'],
                'branch_name': result['branch_name'],
                'branch_removed': result['branch_removed'],
                'errors': result['errors'],
            },
        )
        return result

    def _refresh_pull_request_snapshot(self, task: Task) -> Task:
        if task.source_type != 'pull_request' or not task.source_url:
            return task

        previous_head_sha = task.pr_head_sha
        previous_changed_files = len(task.review_paths)
        try:
            pull_request = fetch_pull_request(task.source_url)
        except Exception as exc:
            self.mission.append_event(
                task_id=task.id,
                kind='task.pull_request_refresh_failed',
                level=EventLevel.ERROR,
                payload={'source_url': task.source_url, 'message': str(exc)},
            )
            raise PullRequestRefreshError(f'failed to refresh latest PR snapshot: {exc}') from exc

        raw_pr = pull_request.raw_pr
        # Field update, not save_task(task): `task` was read before the
        # network call, and saving it would roll back an abort made meanwhile.
        task = self.mission.update_task(
            task.id,
            title=pull_request.task_title,
            description=pull_request.to_description(review_focus=task.review_focus),
            pr_owner=pull_request.owner,
            pr_repo=pull_request.repo,
            pr_number=pull_request.pr_number,
            pr_head_ref=str(raw_pr.get('head', {}).get('ref') or '') or None,
            pr_head_sha=str(raw_pr.get('head', {}).get('sha') or '') or None,
            pr_base_ref=str(raw_pr.get('base', {}).get('ref') or '') or None,
            review_paths=pull_request.changed_files,
        )

        pr_json_path = self.artifacts.write_text(task.id, 'context/pull_request.json', pull_request.to_json())
        pr_md_path = self.artifacts.write_text(
            task.id,
            'context/pull_request.md',
            task.description,
        )
        self.mission.append_event(
            task_id=task.id,
            kind='task.pull_request_refreshed',
            payload={
                'source_url': task.source_url,
                'previous_head_sha': previous_head_sha,
                'head_sha': task.pr_head_sha,
                'head_changed': previous_head_sha != task.pr_head_sha,
                'changed_files': len(task.review_paths),
                'previous_changed_files': previous_changed_files,
                'reviews': len(pull_request.reviews),
                'comments': len(pull_request.issue_comments) + len(pull_request.review_comments),
                'artifact_paths': [pr_json_path, pr_md_path],
            },
        )
        return self.db.get_task(task.id) or task

    def _refresh_local_snapshot(self, task: Task) -> Task:
        """Recapture local changes before every review round."""
        repo = Path(task.repo_path).resolve()
        candidates = (
            self.artifacts.runtime_root / 'tasks',
            self.db.path,
            self.worktrees._worktrees_root_for_repo(task.repo_path),
            Path(f'{self.db.path}-journal'),
            Path(f'{self.db.path}-wal'),
            Path(f'{self.db.path}-shm'),
        )
        generated_paths: list[str] = []
        for path in candidates:
            absolute = path.resolve()
            try:
                generated_paths.append(absolute.relative_to(repo).as_posix())
            except ValueError:
                continue
        previous_head = task.local_head_sha
        previous_snapshot = task.local_snapshot_sha
        head_sha, snapshot_sha, base, changed_files, patch = capture_local_snapshot(
            str(repo), task.pr_base_ref, generated_paths=generated_paths,
        )
        # Field update, not save_task(task): saving the object read before
        # the snapshot would roll back an abort accepted while it was taken.
        task = self.mission.update_task(
            task.id,
            local_head_sha=head_sha,
            local_snapshot_sha=snapshot_sha,
            pr_base_ref=base,
            review_paths=changed_files,
        )
        patch_path = self.artifacts.write_text(task.id, 'context/local_diff.patch', patch)
        self.mission.append_event(
            task_id=task.id,
            kind='task.local_snapshot_refreshed',
            payload={
                'previous_head_sha': previous_head,
                'head_sha': head_sha,
                'previous_snapshot_sha': previous_snapshot,
                'snapshot_sha': snapshot_sha,
                'changed_files': len(changed_files),
                'artifact_path': patch_path,
            },
        )
        return self.db.get_task(task.id) or task
