from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

from app.adapters.base import RunnerAdapter
from app.core.config import load_repo_config
from app.core.context_compiler import ContextCompiler
from app.core.db import Database
from app.core.mission_engine import MissionEngine
from app.core.models import EventLevel, Run, TaskStatus
from app.core.worktree import WorktreeManager


class IdleTimeoutError(RuntimeError):
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
        contexts: ContextCompiler,
    ):
        self.db = db
        self.mission = mission
        self.adapters = adapters
        self.worktrees = worktrees
        self.contexts = contexts
        self._lock = threading.Lock()
        self._active: dict[str, ActiveProcess] = {}

    def start_task(self, task_id: str, prompt: str | None = None) -> Run:
        task = self.db.get_task(task_id)
        if task is None:
            raise KeyError(f'task not found: {task_id}')
        if task.backend not in self.adapters:
            raise KeyError(f'backend adapter not found: {task.backend}')

        self.mission.set_stage(task.id, status=TaskStatus.INGESTING, stage='compile_context')
        branch_name, worktree_path = self.worktrees.ensure_worktree(task)
        task.branch_name = branch_name
        task.worktree_path = worktree_path
        compiled = self.contexts.compile_task(task, worktree_path=worktree_path)
        self.mission.append_event(
            task_id=task.id,
            kind='task.context_compiled',
            payload={
                'markdown_path': compiled.markdown_path,
                'json_path': compiled.json_path,
                'candidate_files': len(compiled.payload.get('candidate_files', [])),
                'worktree_path': worktree_path,
            },
        )

        idle_timeout_sec = self._idle_timeout_seconds(task.repo_path)
        run = Run(task_id=task.id, backend=task.backend, started_at=time.time(), status='running')
        self.db.save_run(run)
        task.last_run_id = run.id
        task.updated_at = time.time()
        self.db.save_task(task)
        self.mission.set_stage(task.id, status=TaskStatus.RUNNING, stage='run_agent')
        worker = threading.Thread(
            target=self._run_task,
            args=(task.id, run.id, prompt or self.contexts.build_prompt(task, compiled), idle_timeout_sec),
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

    def _run_task(self, task_id: str, run_id: str, prompt: str, idle_timeout_sec: int) -> None:
        task = self.db.get_task(task_id)
        run = next((item for item in self.db.list_runs(task_id) if item.id == run_id), None)
        if task is None or run is None:
            return

        proc: subprocess.Popen[str] | None = None
        try:
            adapter = self.adapters[task.backend]
            cmd = adapter.make_command(task=task, prompt=prompt)
            self.mission.append_event(
                task_id=task.id,
                run_id=run.id,
                kind='agent.started',
                payload={
                    'command': cmd,
                    'backend': task.backend,
                    'worktree_path': task.worktree_path,
                    'idle_timeout_sec': idle_timeout_sec,
                },
            )
            proc = subprocess.Popen(
                cmd,
                cwd=task.worktree_path or task.repo_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
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
                                'message': f'No new output for {idle_timeout_sec}s; terminating stalled run',
                                'reason': 'idle_timeout',
                            },
                        )
                        self._terminate_process(proc)
                        raise IdleTimeoutError(f'No new output for {idle_timeout_sec}s')
                    continue

                last_activity_at = time.time()
                events = adapter.parse_stdout_line(line) if stream_name == 'stdout' else adapter.parse_stderr_line(line)
                for event in events:
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
            self.db.save_run(run)

            if exit_code == 0:
                self.mission.set_stage(task.id, status=TaskStatus.WAITING_HUMAN, stage='handoff')
            else:
                self.mission.set_stage(task.id, status=TaskStatus.FAILED, stage='run_agent')

            self.mission.append_event(
                task_id=task.id,
                run_id=run.id,
                kind='agent.finished',
                level=EventLevel.INFO if exit_code == 0 else EventLevel.ERROR,
                payload={'exit_code': exit_code, 'session_id': run.backend_session_id},
            )
        except Exception as exc:
            if proc is not None and proc.poll() is None:
                self._terminate_process(proc)
            run.ended_at = time.time()
            run.exit_code = -2 if isinstance(exc, IdleTimeoutError) else -1
            run.status = 'failed'
            self.db.save_run(run)
            self.mission.append_event(
                task_id=task.id,
                run_id=run.id,
                kind='error',
                level=EventLevel.ERROR,
                payload={'message': str(exc), 'type': exc.__class__.__name__},
            )
            self.mission.set_stage(task.id, status=TaskStatus.FAILED, stage='run_agent')
            self.mission.append_event(
                task_id=task.id,
                run_id=run.id,
                kind='agent.finished',
                level=EventLevel.ERROR,
                payload={
                    'exit_code': run.exit_code,
                    'session_id': run.backend_session_id,
                    'reason': 'idle_timeout' if isinstance(exc, IdleTimeoutError) else 'exception',
                },
            )
        finally:
            with self._lock:
                self._active.pop(task.id, None)

    @staticmethod
    def _terminate_process(proc: subprocess.Popen[str]) -> None:
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

    @staticmethod
    def _idle_timeout_seconds(repo_path: str) -> int:
        cfg = load_repo_config(repo_path)
        execution_cfg = cfg.get('execution', {}) if isinstance(cfg, dict) else {}
        try:
            timeout = int(execution_cfg.get('idle_timeout_sec', 180))
        except (TypeError, ValueError):
            timeout = 180
        return max(timeout, 30)
