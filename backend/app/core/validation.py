from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from app.core.config import load_repo_config
from app.core.db import Database
from app.core.mission_engine import MissionEngine
from app.core.models import CheckRun, TaskStatus
from app.services.artifact_store import ArtifactStore


def _resolve_validation_shell() -> str | None:
    """Pick a shell capable of running typical .amc.yaml commands.

    On Windows ``cmd.exe`` cannot run bash-style commands (``make && npm
    test``) that most repos write; if a bash is available (e.g. shipped with
    Git for Windows) we prefer it. Returning ``None`` keeps the platform
    default shell.
    """
    if sys.platform == 'win32':
        for candidate in ('bash.exe', 'bash'):
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
    return None


_VALIDATION_SHELL = _resolve_validation_shell()


class ValidationService:
    def __init__(self, *, db: Database, mission: MissionEngine, artifacts: ArtifactStore):
        self.db = db
        self.mission = mission
        self.artifacts = artifacts

    def start_validation(self, task_id: str, mode: str | None = None) -> None:
        worker = threading.Thread(target=self._validate_task, args=(task_id, mode), daemon=True)
        worker.start()

    def _validate_task(self, task_id: str, mode: str | None) -> None:
        task = self.db.get_task(task_id)
        if task is None:
            return
        repo_config = load_repo_config(task.repo_path)
        validation_config = repo_config.get('validation', {})
        default_mode = validation_config.get('default_mode', 'standard')
        selected_mode = mode or default_mode
        checks = validation_config.get('checks', {})
        self.mission.set_stage(task.id, status=TaskStatus.VALIDATING, stage='validate')
        if not checks:
            self.mission.append_event(task_id=task.id, kind='validation.finished', payload={'status': 'blocked', 'reason': 'no checks configured'})
            self.mission.set_stage(task.id, status=TaskStatus.WAITING_HUMAN, stage='handoff')
            return
        for name, config in checks.items():
            command = str(config.get('command', '')).strip()
            modes = config.get('modes', ['standard'])
            required = bool(config.get('required', False))
            timeout_sec = float(config.get('timeout', 600))
            if selected_mode not in modes:
                continue
            check = CheckRun(task_id=task.id, name=name, command=command, status='blocked' if not command else 'running')
            self.db.save_check_run(check)
            if not command:
                self.mission.append_event(task_id=task.id, kind='validation.finished', payload={'check': name, 'status': 'blocked', 'reason': 'empty command'})
                continue
            self.mission.append_event(task_id=task.id, kind='validation.started', payload={'check': name, 'command': command, 'required': required})
            start = time.time()
            stdout_text = ''
            stderr_text = ''
            exit_code: int | None = None
            error_reason: str | None = None
            try:
                run_kwargs: dict = dict(
                    shell=True,
                    cwd=task.worktree_path or task.repo_path,
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    timeout=timeout_sec,
                )
                if _VALIDATION_SHELL:
                    run_kwargs['executable'] = _VALIDATION_SHELL
                result = subprocess.run(command, **run_kwargs)
                stdout_text = result.stdout or ''
                stderr_text = result.stderr or ''
                exit_code = result.returncode
            except subprocess.TimeoutExpired as exc:
                stdout_text = exc.stdout.decode('utf-8', 'replace') if isinstance(exc.stdout, bytes) else (exc.stdout or '')
                stderr_text = exc.stderr.decode('utf-8', 'replace') if isinstance(exc.stderr, bytes) else (exc.stderr or '')
                error_reason = f'timeout after {timeout_sec}s'
            except OSError as exc:
                error_reason = f'failed to spawn shell: {exc}'

            if error_reason and exit_code is None:
                exit_code = -1
                stderr_text = (stderr_text + ('\n' if stderr_text else '') + error_reason).strip()
            check.status = 'passed' if exit_code == 0 else 'failed'
            check.exit_code = exit_code
            check.duration_sec = time.time() - start
            check.stdout_path = self.artifacts.write_text(task.id, f'checks/{name}.stdout.log', stdout_text)
            check.stderr_path = self.artifacts.write_text(task.id, f'checks/{name}.stderr.log', stderr_text)
            self.db.save_check_run(check)
            payload = {
                'check': name,
                'status': check.status,
                'exit_code': check.exit_code,
                'required': required,
            }
            if error_reason:
                payload['error'] = error_reason
            self.mission.append_event(
                task_id=task.id,
                kind='validation.finished',
                payload=payload,
            )
        self.mission.set_stage(task.id, status=TaskStatus.WAITING_HUMAN, stage='handoff')
