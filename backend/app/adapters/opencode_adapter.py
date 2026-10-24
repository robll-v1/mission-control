from __future__ import annotations

from app.adapters.base import RunnerAdapter, resolve_executable
from app.core.models import Task


class OpenCodeAdapter(RunnerAdapter):
    name = 'opencode'
    executable = 'opencode'

    def __init__(self, model: str | None = None, variant: str | None = None):
        self.model = model
        self.variant = variant

    def probe_command(self) -> list[str] | None:
        return [resolve_executable('opencode'), 'run', '--format', 'json',
                'Reply with exactly one word: OK']

    def make_command(self, *, task: Task, prompt: str) -> list[str]:
        # The working directory comes from the process cwd, which
        # ExecutionService already sets to the task worktree. Current opencode
        # (v2) has no --dir flag and expresses variants as model#variant, so
        # passing them as separate args makes it print usage and exit.
        cmd = [resolve_executable('opencode'), 'run', '--format', 'json']
        model = self.model
        if model and self.variant:
            model = f'{model}#{self.variant}'
        if model:
            cmd.extend(['--model', model])
        cmd.append(prompt)
        return cmd
