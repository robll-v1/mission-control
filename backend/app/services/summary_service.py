from __future__ import annotations

from app.core.db import Database
from app.services.artifact_store import ArtifactStore


class SummaryService:
    def __init__(self, db: Database, artifacts: ArtifactStore):
        self.db = db
        self.artifacts = artifacts

    def export_summary(self, task_id: str) -> str:
        task = self.db.get_task(task_id)
        events = self.db.list_events(task_id)
        runs = self.db.list_runs(task_id)
        checks = self.db.list_check_runs(task_id)
        if task is None:
            raise KeyError(f'task not found: {task_id}')
        lines = [
            f'# Task Summary: {task.title}',
            '',
            f'- Status: {task.status}',
            f'- Stage: {task.current_stage}',
            f'- Backend: {task.backend}',
            f'- Repo: {task.repo_path}',
            '',
            '## Description',
            task.description or '(empty)',
            '',
            '## Runs',
        ]
        if runs:
            lines.extend(f'- {run.backend}: status={run.status} exit={run.exit_code} session={run.backend_session_id or "-"}' for run in runs)
        else:
            lines.append('- none')
        lines.extend(['', '## Validation Checks'])
        if checks:
            lines.extend(f'- {check.name}: status={check.status} exit={check.exit_code}' for check in checks)
        else:
            lines.append('- none')
        lines.extend(['', '## Recent Events'])
        if events:
            for event in events[-20:]:
                lines.append(f'- #{event.seq} {event.kind}: {event.payload}')
        else:
            lines.append('- none')
        return self.artifacts.write_text(task_id, 'exports/summary.md', '\n'.join(lines) + '\n')
