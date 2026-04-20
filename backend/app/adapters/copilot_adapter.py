"""GitHub Copilot CLI adapter — uses `gh copilot -- -p` with json output."""

from __future__ import annotations

import json

from app.adapters.base import AdapterEvent, RunnerAdapter
from app.core.models import Task


class CopilotAdapter(RunnerAdapter):
    name = 'copilot'

    def __init__(self, model: str | None = None, variant: str | None = None):
        self.model = model
        self.variant = variant

    def make_command(self, *, task: Task, prompt: str) -> list[str]:
        work_dir = task.worktree_path or task.repo_path
        cmd = [
            'gh', 'copilot', '--',
            '-p', prompt,
            '--output-format', 'json',
            '--allow-all-tools',
            '--add-dir', work_dir,
        ]
        if self.model:
            cmd.extend(['--model', self.model])
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
        data = obj.get('data', {})

        if msg_type == 'assistant.turn_start':
            return [AdapterEvent(kind='agent.step_start', payload={
                'type': 'turn_start',
                'turn_id': data.get('turnId'),
            })]

        if msg_type == 'assistant.message':
            content = data.get('content', '').strip()
            if not content:
                return []
            events: list[AdapterEvent] = []
            events.append(AdapterEvent(kind='agent.text', payload={
                'text': content,
                'message_id': data.get('messageId'),
            }))
            # Handle tool requests within messages
            for tool_req in data.get('toolRequests', []):
                events.append(AdapterEvent(kind='agent.tool_use', payload={
                    'tool': tool_req.get('tool', {}).get('name', ''),
                    'status': 'requested',
                    'title': tool_req.get('tool', {}).get('name', ''),
                }))
            return events

        if msg_type == 'assistant.tool_result':
            return [AdapterEvent(kind='agent.tool_use', payload={
                'tool': data.get('toolName', ''),
                'status': 'completed',
                'output_preview': str(data.get('result', ''))[:360],
            })]

        if msg_type == 'assistant.turn_end':
            return [AdapterEvent(kind='agent.step_finish', payload={
                'reason': 'turn_end',
                'turn_id': data.get('turnId'),
            })]

        if msg_type == 'result':
            return [AdapterEvent(kind='agent.step_finish', payload={
                'reason': 'completed',
                'exit_code': obj.get('exitCode'),
                'session_id': obj.get('sessionId'),
            })]

        # Ignore ephemeral session events (mcp_server_status, tools_updated, etc.)
        return []
