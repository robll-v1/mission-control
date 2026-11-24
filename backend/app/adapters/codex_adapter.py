"""OpenAI Codex CLI adapter — runs `codex exec --json` in a read-only sandbox."""

from __future__ import annotations

import json
import os
import re
import shlex
from typing import Any

from app.adapters.base import AdapterEvent, RunnerAdapter, resolve_executable
from app.core.models import Task
from app.core.review_policy import BLOCKED_MARKER, forbidden_command_reason

_SHELLS = {'bash', 'zsh', 'sh', 'dash'}
_SAFE_REASONING_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$')


def _validate_reasoning(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if not _SAFE_REASONING_RE.fullmatch(cleaned):
        raise ValueError(f'invalid reasoning effort value: {value!r}')
    return cleaned


def _shell_script(command: str) -> str:
    """The script inside codex's `<shell> -lc '<script>'` wrapper, else the command.

    The policy pattern needs whitespace before a tool name, so in the wrapped
    form `npm test` follows a quote and would never match.
    """
    try:
        argv = shlex.split(command)
    except ValueError:
        return command
    if len(argv) >= 3 and os.path.basename(argv[0]) in _SHELLS and argv[1] in ('-c', '-lc', '-cl'):
        return argv[2]
    return command


class CodexAdapter(RunnerAdapter):
    name = 'codex'
    executable = 'codex'
    supports_reasoning = True

    def __init__(self, model: str | None = None, variant: str | None = None, reasoning: str | None = None):
        self.model = model
        eff = _validate_reasoning(reasoning if reasoning is not None else variant)
        self.reasoning = eff
        self.variant = eff

    def _exec_args(self, work_dir: str) -> list[str]:
        # codex-cli 0.169 rejects --full-auto (deprecated in 0.128, since
        # removed) and --cwd (the flag is -C/--cd), exiting 2 before a review
        # starts. A review only reads: model shell commands run in the
        # read-only sandbox, which is also exec's default and is set here so
        # a user config cannot widen it. On macOS codex runs those commands
        # in a login shell, which can put /usr/bin ahead of the PATH shims,
        # so for codex the sandbox (no writes, no network) is the guard and
        # the static policy check only flags commands.
        args = [
            resolve_executable('codex'), 'exec', '--json',
            '--sandbox', 'read-only',
            '--cd', work_dir,
        ]
        if self.model:
            args.extend(['--model', self.model])
        if self.reasoning:
            args.extend(['-c', f'model_reasoning_effort={self.reasoning}'])
        return args

    def probe_command(self) -> list[str] | None:
        # Parses the same flags as a round without starting a session, so a
        # CLI update that drops one fails preflight instead of the round.
        return [*self._exec_args('.'), '--version']

    def doctor_command(self) -> list[str]:
        return [*self._exec_args('.'), '--version']

    def make_command(self, *, task: Task, prompt: str) -> list[str]:
        return [*self._exec_args(task.worktree_path or task.repo_path), prompt]

    def parse_stdout_line(self, line: str) -> list[AdapterEvent]:
        stripped = line.strip()
        if not stripped:
            return []
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            return [AdapterEvent(kind='agent.text', payload={'text': stripped})]
        if not isinstance(obj, dict):
            return []

        msg_type = obj.get('type', '')

        if msg_type == 'thread.started':
            payload: dict[str, Any] = {'type': 'thread_start'}
            thread_id = str(obj.get('thread_id') or '').strip()
            if thread_id:
                # Kept as the run's backend session (`codex exec resume <id>`).
                payload['session_id'] = thread_id
            return [AdapterEvent(kind='agent.step_start', payload=payload)]

        if msg_type == 'turn.started':
            return [AdapterEvent(kind='agent.step_start', payload={'type': 'turn_start'})]

        if msg_type == 'item.completed':
            item = obj.get('item') if isinstance(obj.get('item'), dict) else {}
            return self._item_events(item)

        if msg_type == 'turn.completed':
            usage = obj.get('usage') if isinstance(obj.get('usage'), dict) else {}
            return [AdapterEvent(kind='agent.step_finish', payload={
                'reason': 'turn_completed',
                'token_total': (usage.get('input_tokens') or 0) + (usage.get('output_tokens') or 0),
                'token_output': usage.get('output_tokens'),
                'token_reasoning': usage.get('reasoning_output_tokens'),
            })]

        if msg_type == 'turn.failed':
            # Without this a failed turn (bad model name, auth, quota) left a
            # failed round with no reason anywhere in the events.
            error = obj.get('error') if isinstance(obj.get('error'), dict) else {}
            return [AdapterEvent(kind='agent.error', level='error', payload={
                'error_type': 'turn.failed',
                'error_message': str(error.get('message') or 'turn failed'),
            })]

        if msg_type == 'error':
            # Also used for transient "Reconnecting... 1/5" notices; a fatal
            # error is followed by turn.failed or a non-zero exit.
            return [AdapterEvent(kind='agent.error', level='warning', payload={
                'error_type': 'stream',
                'error_message': str(obj.get('message') or ''),
            })]

        return []

    def _item_events(self, item: dict[str, Any]) -> list[AdapterEvent]:
        item_type = item.get('type', '')
        if item_type == 'agent_message':
            text = str(item.get('text') or '').strip()
            if not text:
                return []
            payload: dict[str, Any] = {'text': text}
            phase = item.get('phase')
            if isinstance(phase, str) and phase:
                # 'commentary' marks interim progress text; the result
                # service never takes it as the review conclusion.
                payload['phase'] = phase
            return [AdapterEvent(kind='agent.text', payload=payload)]
        if item_type == 'command_execution':
            return [self._command_event(item)]
        if item_type == 'mcp_tool_call':
            error = item.get('error') if isinstance(item.get('error'), dict) else {}
            payload = {
                'tool': f"mcp:{item.get('server') or '?'}/{item.get('tool') or '?'}",
                'status': item.get('status'),
                'title': 'MCP tool call',
                'warning_reason': 'MCP tools run outside the codex read-only sandbox',
            }
            if error.get('message'):
                payload['output_preview'] = self._truncate(str(error['message']), 260)
            return [AdapterEvent(kind='agent.tool_use', level='warning', payload=payload)]
        if item_type == 'web_search':
            return [AdapterEvent(kind='agent.tool_use', level='warning', payload={
                'tool': 'web_search',
                'status': 'completed',
                'title': 'web search',
                'query': str(item.get('query') or ''),
                'warning_reason': 'web search during a static review',
            })]
        if item_type == 'file_change':
            changes = item.get('changes') if isinstance(item.get('changes'), list) else []
            paths = [str(c['path']) for c in changes if isinstance(c, dict) and c.get('path')]
            return [AdapterEvent(kind='agent.tool_use', level='warning', payload={
                'tool': 'file_change',
                'status': item.get('status'),
                'title': 'file change',
                'paths': paths[:20],
                'warning_reason': 'a review should not edit files',
            })]
        if item_type == 'error':
            message = str(item.get('message') or '').strip()
            if message:
                return [AdapterEvent(kind='agent.error', level='warning',
                                     payload={'error_type': 'item', 'error_message': message})]
        # reasoning and todo_list are not needed to follow or judge a review.
        return []

    def _command_event(self, item: dict[str, Any]) -> AdapterEvent:
        command = str(item.get('command') or '').strip()
        output = str(item.get('aggregated_output') or '').strip()
        exit_code = item.get('exit_code')
        payload: dict[str, Any] = {
            'tool': 'bash',
            'status': item.get('status'),
            'title': 'command',
            'command_preview': self._truncate(command, 220),
            'exit_code': exit_code,
        }
        if output:
            payload['output_preview'] = self._truncate(output, 360)
        level = 'info'
        reason = forbidden_command_reason(_shell_script(command))
        if reason or BLOCKED_MARKER in output:
            payload['policy_violation'] = True
            payload['warning_reason'] = reason or 'command was blocked by static review policy'
            level = 'error'
        elif isinstance(exit_code, int) and exit_code != 0:
            payload['warning_reason'] = 'command exited non-zero'
            level = 'warning'
        return AdapterEvent(kind='agent.tool_use', level=level, payload=payload)
