from __future__ import annotations

import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from app.adapters.base import RunnerAdapter
from app.core.config import load_repo_config
from app.core.context_compiler import ContextCompiler
from app.core.db import Database
from app.core.github_ingest import fetch_pull_request
from app.core.mission_engine import MissionEngine
from app.core.models import EventLevel, Run, Task, TaskStatus
from app.core.review_policy import prepare_static_review_env
from app.core.worktree import WorktreeManager
from app.services.artifact_store import ArtifactStore
from app.services.review_result_service import ReviewResultService


class IdleTimeoutError(RuntimeError):
    pass


class PreflightError(RuntimeError):
    pass


class PullRequestRefreshError(RuntimeError):
    pass


@dataclass
class ActiveProcess:
    task_id: str
    run_id: str
    process: subprocess.Popen[str] | None
    worker: threading.Thread


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
    ):
        self.db = db
        self.mission = mission
        self.adapters = adapters
        self.worktrees = worktrees
        self.artifacts = artifacts
        self.contexts = contexts
        self._lock = threading.Lock()
        self._active: dict[str, ActiveProcess] = {}

    def start_task(self, task_id: str, prompt: str | None = None, review_note: str | None = None, language: str = 'zh') -> Run:
        task = self.db.get_task(task_id)
        if task is None:
            raise KeyError(f'task not found: {task_id}')
        if task.backend not in self.adapters:
            raise KeyError(f'backend adapter not found: {task.backend}')
        with self._lock:
            if task_id in self._active:
                raise RuntimeError('a review round is already in progress for this PR')

        self._preflight_check(task)

        task = self._refresh_pull_request_snapshot(task)
        task = self.mission.set_stage(task.id, status=TaskStatus.INGESTING, stage='prepare_review')
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
        task.last_run_id = run.id
        task.updated_at = time.time()
        self.db.save_task(task)
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
            ),
            daemon=True,
        )
        placeholder = ActiveProcess(task_id=task.id, run_id=run.id, process=None, worker=worker)
        with self._lock:
            self._active[task.id] = placeholder
        worker.start()
        return run

    def abort_task(self, task_id: str) -> bool:
        with self._lock:
            active = self._active.get(task_id)
        if active is None or active.process is None:
            return False
        self._terminate_process(active.process)
        self.mission.set_stage(task_id, status=TaskStatus.ABORTED, stage='aborted')
        self.mission.append_event(
            task_id=task_id,
            run_id=active.run_id,
            kind='control.accepted',
            payload={'action': 'abort'},
        )
        return True

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

    def _run_task(self, task_id: str, run_id: str, round_index: int, prompt: str, idle_timeout_sec: int) -> None:
        task = self.db.get_task(task_id)
        run = next((item for item in self.db.list_runs(task_id) if item.id == run_id), None)
        if task is None or run is None:
            return

        proc: subprocess.Popen[str] | None = None
        try:
            adapter = self.adapters[task.backend]
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
                bufsize=1,
                start_new_session=(sys.platform != 'win32'),
                **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {}),
                env=prepare_static_review_env(self.worktrees.runtime_root),
            )
            with self._lock:
                self._active[task.id] = ActiveProcess(task_id=task.id, run_id=run.id, process=proc, worker=threading.current_thread())

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
            run.review_result = ReviewResultService.extract_result(events=self.db.list_events(task_id), run=run)
            parse_result_ms = int((time.perf_counter() - parse_started_at) * 1000)
            run.metrics.update({
                'llm_wall_time_ms': int((time.perf_counter() - llm_started_at) * 1000),
                'parse_result_ms': parse_result_ms,
                'agent_event_count': agent_event_count,
                'agent_text_bytes': agent_text_bytes,
                'round_duration_ms': int((run.ended_at - run.started_at) * 1000),
            })
            self.db.save_run(run)
            task.latest_review_result = ReviewResultService.latest_result(self.db.list_runs(task.id))
            self.db.save_task(task)

            if exit_code == 0:
                self.mission.set_stage(task.id, status=TaskStatus.WAITING_HUMAN, stage='awaiting_next_round')
            else:
                self.mission.set_stage(task.id, status=TaskStatus.FAILED, stage='review_failed')

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
            run.review_result = ReviewResultService.extract_result(events=self.db.list_events(task_id), run=run)
            parse_result_ms = int((time.perf_counter() - parse_started_at) * 1000)
            llm_elapsed_ms = int((time.perf_counter() - llm_started_at) * 1000) if 'llm_started_at' in locals() else 0
            run.metrics.update({
                'llm_wall_time_ms': llm_elapsed_ms,
                'parse_result_ms': parse_result_ms,
                'agent_event_count': agent_event_count if 'agent_event_count' in locals() else 0,
                'agent_text_bytes': agent_text_bytes if 'agent_text_bytes' in locals() else 0,
                'round_duration_ms': int((run.ended_at - run.started_at) * 1000),
            })
            self.db.save_run(run)
            task.latest_review_result = ReviewResultService.latest_result(self.db.list_runs(task.id))
            self.db.save_task(task)
            self.mission.append_event(
                task_id=task.id,
                run_id=run.id,
                kind='error',
                level=EventLevel.ERROR,
                payload={'message': str(exc), 'type': exc.__class__.__name__, 'round_index': round_index},
            )
            self.mission.set_stage(task.id, status=TaskStatus.FAILED, stage='review_failed')
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
                    'reason': 'idle_timeout' if isinstance(exc, IdleTimeoutError) else 'exception',
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

    def _preflight_check(self, task: Task) -> None:
        adapter = self.adapters.get(task.backend)
        if adapter is None:
            raise PreflightError(f'backend adapter not configured: {task.backend}')

        backend_name = adapter.name
        executable = backend_name
        if not shutil.which(executable):
            raise PreflightError(
                f'{executable} is not installed or not in PATH. '
                f'Install it before starting a review.'
            )

        try:
            result = subprocess.run(
                [executable, 'run', '--format', 'json', 'Reply with exactly one word: OK'],
                capture_output=True, text=True, timeout=60,
                cwd=task.repo_path,
            )
            if result.returncode != 0:
                stderr_preview = (result.stderr or '').strip()[:300]
                raise PreflightError(
                    f'{executable} preflight test failed (exit {result.returncode}). '
                    f'Check your API key and configuration. stderr: {stderr_preview}'
                )
        except subprocess.TimeoutExpired:
            raise PreflightError(
                f'{executable} preflight test timed out after 60s. '
                f'The LLM backend may be unreachable.'
            )
        except FileNotFoundError:
            raise PreflightError(f'{executable} executable not found.')

        self.mission.append_event(
            task_id=task.id,
            kind='task.preflight_passed',
            payload={'backend': backend_name},
        )

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
        task.title = pull_request.task_title
        task.description = pull_request.to_description(review_focus=task.review_focus)
        task.pr_owner = pull_request.owner
        task.pr_repo = pull_request.repo
        task.pr_number = pull_request.pr_number
        task.pr_head_ref = str(raw_pr.get('head', {}).get('ref') or '') or None
        task.pr_head_sha = str(raw_pr.get('head', {}).get('sha') or '') or None
        task.pr_base_ref = str(raw_pr.get('base', {}).get('ref') or '') or None
        task.review_paths = pull_request.changed_files
        task.updated_at = time.time()
        self.db.save_task(task)

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
