from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from app.core.config import load_repo_config
from app.core.db import Database
from app.core.mission_engine import MissionEngine
from app.core.models import CheckRun, TaskStatus
from app.services.artifact_store import ArtifactStore


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
            if selected_mode not in modes:
                continue
            check = CheckRun(task_id=task.id, name=name, command=command, status='blocked' if not command else 'running')
            self.db.save_check_run(check)
            if not command:
                self.mission.append_event(task_id=task.id, kind='validation.finished', payload={'check': name, 'status': 'blocked', 'reason': 'empty command'})
                continue
            self.mission.append_event(task_id=task.id, kind='validation.started', payload={'check': name, 'command': command, 'required': required})
            start = time.time()
            result = subprocess.run(
                command,
                shell=True,
                cwd=task.worktree_path or task.repo_path,
                capture_output=True,
                text=True,
            )
            check.status = 'passed' if result.returncode == 0 else 'failed'
            check.exit_code = result.returncode
            check.duration_sec = time.time() - start
            check.stdout_path = self.artifacts.write_text(task.id, f'checks/{name}.stdout.log', result.stdout)
            check.stderr_path = self.artifacts.write_text(task.id, f'checks/{name}.stderr.log', result.stderr)
            self.db.save_check_run(check)
            self.mission.append_event(
                task_id=task.id,
                kind='validation.finished',
                payload={
                    'check': name,
                    'status': check.status,
                    'exit_code': check.exit_code,
                    'required': required,
                },
            )
        self.mission.set_stage(task.id, status=TaskStatus.WAITING_HUMAN, stage='handoff')
