"""OpenAI Codex CLI adapter — uses `codex exec --json --full-auto`."""

from __future__ import annotations

import json

from app.adapters.base import AdapterEvent, RunnerAdapter
from app.core.models import Task


class CodexAdapter(RunnerAdapter):
    name = 'codex'

    def __init__(self, model: str | None = None, variant: str | None = None):
        self.model = model
        self.variant = variant

    def make_command(self, *, task: Task, prompt: str) -> list[str]:
        work_dir = task.worktree_path or task.repo_path
        cmd = ['codex', 'exec', '--json', '--full-auto']
        if self.model:
            cmd.extend(['-c', f'model="{self.model}"'])
        cmd.extend(['--cwd', work_dir, prompt])
        return cmd

    def parse_stdout_line(self, line: str) -> list[AdapterEvent]:
        stripped = line.strip()
        if not stripped:
            return []
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            return [AdapterEvent(kind='agent.text', payload={'text': stripped})]

        msg_type = obj.get('type', '')

        if msg_type == 'thread.started':
            return [AdapterEvent(kind='agent.step_start', payload={
                'type': 'thread_start',
                'thread_id': obj.get('thread_id'),
            })]

        if msg_type == 'turn.started':
            return [AdapterEvent(kind='agent.step_start', payload={'type': 'turn_start'})]

        if msg_type == 'item.completed':
            item = obj.get('item', {})
            item_type = item.get('type', '')
            if item_type == 'agent_message':
                text = item.get('text', '').strip()
                if text:
                    return [AdapterEvent(kind='agent.text', payload={'text': text})]
            elif item_type == 'tool_call':
                return [AdapterEvent(kind='agent.tool_use', payload={
                    'tool': item.get('name', ''),
                    'status': 'completed',
                    'title': item.get('name', ''),
                    'output_preview': str(item.get('output', ''))[:360],
                })]
            elif item_type == 'tool_output':
                return [AdapterEvent(kind='agent.tool_use', payload={
                    'tool': 'output',
                    'status': 'completed',
                    'output_preview': str(item.get('output', ''))[:360],
                })]
            return []

        if msg_type == 'turn.completed':
            usage = obj.get('usage', {})
            return [AdapterEvent(kind='agent.step_finish', payload={
                'reason': 'turn_completed',
                'token_total': usage.get('input_tokens', 0) + usage.get('output_tokens', 0),
                'token_output': usage.get('output_tokens'),
            })]

        return []
