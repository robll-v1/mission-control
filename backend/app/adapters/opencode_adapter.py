from __future__ import annotations

from app.adapters.base import RunnerAdapter, resolve_executable
from app.core.models import Task


class OpenCodeAdapter(RunnerAdapter):
    name = 'opencode'

    def __init__(self, model: str | None = None, variant: str | None = None):
        self.model = model
        self.variant = variant

    def make_command(self, *, task: Task, prompt: str) -> list[str]:
        work_dir = task.worktree_path or task.repo_path
        cmd = [resolve_executable('opencode'), 'run', '--dir', work_dir, '--format', 'json']
        if self.model:
            cmd.extend(['--model', self.model])
        if self.variant:
            cmd.extend(['--variant', self.variant])
        cmd.append(prompt)
        return cmd
