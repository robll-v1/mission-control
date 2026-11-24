from __future__ import annotations

from app.adapters.base import RunnerAdapter, resolve_executable, validate_model_name, validate_reasoning_level
from app.core.models import Task


class OpenCodeAdapter(RunnerAdapter):
    name = 'opencode'
    executable = 'opencode'
    supports_reasoning = False

    def __init__(self, model: str | None = None, variant: str | None = None, reasoning: str | None = None):
        self.model = validate_model_name(model)
        eff = validate_reasoning_level(reasoning if reasoning is not None else variant)
        # OpenCode only passes variants as `model#variant`; without a model the
        # CLI receives no variant/reasoning flag at all.
        self.variant = eff if self.model else None
        self.reasoning = eff if self.model else None

    def probe_command(self) -> list[str] | None:
        return [resolve_executable('opencode'), 'run', '--format', 'json',
                'Reply with exactly one word: OK']

    def doctor_command(self) -> list[str]:
        # Never call the model during static `Check agent` / `doctor`.
        return [resolve_executable('opencode'), '--version']

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
