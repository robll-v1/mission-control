"""Claude Code CLI adapter — uses `claude -p` with stream-json output."""

from __future__ import annotations

import json

from app.adapters.base import (
    AdapterEvent,
    RunnerAdapter,
    resolve_executable,
    validate_model_name,
    validate_reasoning_level,
)
from app.core.models import Task


class ClaudeCodeAdapter(RunnerAdapter):
    name = 'claude-code'
    executable = 'claude'
    supports_reasoning = True

    def __init__(self, model: str | None = None, variant: str | None = None, reasoning: str | None = None):
        self.model = validate_model_name(model)
        eff = validate_reasoning_level(reasoning if reasoning is not None else variant)
        self.reasoning = eff
        self.variant = eff

    def _selection_args(self) -> list[str]:
        args: list[str] = []
        if self.model:
            args.extend(['--model', self.model])
        if self.reasoning:
            args.extend(['--effort', self.reasoning])
        return args

    def probe_command(self) -> list[str] | None:
        return [resolve_executable('claude'), *self._selection_args(), '--version']

    def doctor_command(self) -> list[str]:
        # Fixed arguments only: avoids passing HTTP parameters into Windows
        # .cmd/.bat wrappers; model and reasoning are validated in-process.
        return [resolve_executable('claude'), '--version']

    def make_command(self, *, task: Task, prompt: str) -> list[str]:
        work_dir = task.worktree_path or task.repo_path
        cmd = [
            resolve_executable('claude'), '-p', prompt,
            '--output-format', 'stream-json',
            '--verbose',
            '--permission-mode', 'bypassPermissions',
            '--add-dir', work_dir,
            *self._selection_args(),
        ]
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

        if msg_type == 'system' and obj.get('subtype') == 'init':
            return [AdapterEvent(kind='agent.step_start', payload={
                'type': 'init',
                'model': obj.get('model'),
                'session_id': obj.get('session_id'),
            })]

        if msg_type == 'assistant':
            message = obj.get('message', {})
            content_parts = message.get('content', [])
            events: list[AdapterEvent] = []
            for part in content_parts:
                if part.get('type') == 'text':
                    text = part.get('text', '').strip()
                    if text:
                        events.append(AdapterEvent(kind='agent.text', payload={
                            'text': text,
                            'session_id': obj.get('session_id'),
                        }))
                elif part.get('type') == 'tool_use':
                    events.append(AdapterEvent(kind='agent.tool_use', payload={
                        'tool': part.get('name', ''),
                        'status': 'completed',
                        'title': part.get('name', ''),
                        'session_id': obj.get('session_id'),
                    }))
            return events

        if msg_type == 'tool_result':
            content = obj.get('content', '')
            if isinstance(content, list):
                content = ' '.join(p.get('text', '') for p in content if p.get('type') == 'text')
            return [AdapterEvent(kind='agent.tool_use', payload={
                'tool': 'result',
                'status': 'completed',
                'output_preview': str(content)[:360],
            })]

        if msg_type == 'result':
            result_text = obj.get('result', '')
            events = []
            if result_text:
                events.append(AdapterEvent(kind='agent.text', payload={'text': result_text}))
            events.append(AdapterEvent(kind='agent.step_finish', payload={
                'reason': obj.get('stop_reason', 'end_turn'),
                'cost': obj.get('total_cost_usd'),
                'duration_ms': obj.get('duration_ms'),
            }))
            return events

        return []
