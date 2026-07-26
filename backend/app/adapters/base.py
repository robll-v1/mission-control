from __future__ import annotations

import json
import shutil
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.core.review_policy import BLOCKED_MARKER, forbidden_command_reason
from app.core.models import Task


def resolve_executable(name: str) -> str:
    """Return a launchable path for ``name``, falling back to the bare name.

    On Windows ``shutil.which`` honours PATHEXT, so npm/codex/gh installed as
    ``.cmd`` or ``.bat`` shims resolve correctly. Falling back to the bare name
    preserves macOS/Linux behaviour and keeps preflight error messages familiar
    when the binary is genuinely missing.
    """
    resolved = shutil.which(name)
    if resolved:
        return resolved
    if sys.platform == 'win32':
        for ext in ('.cmd', '.exe', '.bat'):
            resolved = shutil.which(name + ext)
            if resolved:
                return resolved
    return name


@dataclass
class AdapterEvent:
    kind: str
    level: str = 'info'
    payload: dict[str, Any] = field(default_factory=dict)


class RunnerAdapter(ABC):
    #: Backend key used in config and on ``Task.backend`` (e.g. ``claude-code``).
    name: str
    #: Name of the program to launch. Often differs from :attr:`name` —
    #: ``claude-code`` runs ``claude``, ``copilot`` runs ``gh``. Preflight must
    #: probe *this*, not :attr:`name`.
    executable: str = ''

    @abstractmethod
    def make_command(self, *, task: Task, prompt: str) -> list[str]:
        raise NotImplementedError

    @classmethod
    def executable_name(cls) -> str:
        """Program this adapter launches; falls back to the backend key."""
        return cls.executable or cls.name

    def probe_command(self) -> list[str] | None:
        """A cheap invocation proving the CLI is usable, or ``None`` to skip.

        Returning ``None`` means "existence on PATH is the only check we can
        make". That is preferable to running an invocation the CLI does not
        understand, which produces a confusing failure that looks like a
        credentials problem.
        """
        return None

    def parse_stdout_line(self, line: str) -> list[AdapterEvent]:
        stripped = line.strip()
        if not stripped:
            return []
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            return [AdapterEvent(kind='agent.text', payload={'text': stripped})]
        event_type = obj.get('type', 'event')
        session_id = str(obj.get('sessionID') or '').strip() or None
        if event_type == 'text':
            text = self._part_text(obj)
            if not text:
                return []
            payload = {'text': text}
            if session_id:
                payload['session_id'] = session_id
            return [AdapterEvent(kind='agent.text', payload=payload)]
        if event_type == 'tool_use':
            return [self._tool_event(obj, session_id=session_id)]
        if event_type == 'step_start':
            payload: dict[str, Any] = {'type': 'step_start'}
            part = obj.get('part', {}) if isinstance(obj.get('part'), dict) else {}
            if session_id:
                payload['session_id'] = session_id
            if part.get('messageID'):
                payload['message_id'] = part.get('messageID')
            if part.get('snapshot'):
                payload['snapshot'] = part.get('snapshot')
            return [AdapterEvent(kind='agent.step_start', payload=payload)]
        if event_type == 'step_finish':
            return [self._step_finish_event(obj, session_id=session_id)]
        payload = {'type': event_type}
        if session_id:
            payload['session_id'] = session_id
        return [AdapterEvent(kind=f'agent.{event_type}', payload=payload)]

    def parse_stderr_line(self, line: str) -> list[AdapterEvent]:
        stripped = line.rstrip()
        if not stripped:
            return []
        return [AdapterEvent(kind='agent.stderr', level='warning', payload={'text': stripped})]

    @staticmethod
    def _part_text(obj: dict[str, Any]) -> str:
        part = obj.get('part')
        if isinstance(part, dict) and isinstance(part.get('text'), str):
            return str(part.get('text')).strip()
        return ''

    def _tool_event(self, obj: dict[str, Any], *, session_id: str | None) -> AdapterEvent:
        part = obj.get('part', {}) if isinstance(obj.get('part'), dict) else {}
        state = part.get('state', {}) if isinstance(part.get('state'), dict) else {}
        tool = str(part.get('tool') or '')
        metadata = state.get('metadata', {}) if isinstance(state.get('metadata'), dict) else {}
        raw_input = state.get('input', {}) if isinstance(state.get('input'), dict) else {}
        payload: dict[str, Any] = {
            'tool': tool,
            'status': state.get('status'),
            'title': state.get('title') or metadata.get('description') or '',
        }
        if session_id:
            payload['session_id'] = session_id

        level = 'info'
        if tool == 'bash':
            command = str(raw_input.get('command') or '').strip()
            payload['command_preview'] = self._truncate(command, 220)
            exit_code = metadata.get('exit')
            payload['exit_code'] = exit_code
            output = str(metadata.get('output') or state.get('output') or '').strip()
            if output:
                payload['output_preview'] = self._truncate(output, 360)
            reason = forbidden_command_reason(command)
            if reason:
                payload['policy_violation'] = True
                payload['warning_reason'] = reason
                level = 'error'
            elif BLOCKED_MARKER in output:
                payload['policy_violation'] = True
                payload['warning_reason'] = 'command was blocked by static review policy'
                level = 'error'
            elif isinstance(exit_code, int) and exit_code != 0:
                payload['warning_reason'] = 'command exited non-zero'
                level = 'warning'
            elif 'timeout' in output.lower() or 'timed out' in output.lower():
                payload['warning_reason'] = 'command timed out'
                level = 'warning'
        elif tool == 'read':
            payload['file_path'] = raw_input.get('filePath')
        elif tool == 'grep':
            payload['pattern'] = raw_input.get('pattern')
            payload['path'] = raw_input.get('path')
            matches = metadata.get('matches')
            if matches is not None:
                payload['matches'] = matches
            output = str(state.get('output') or '').strip()
            if output and output != 'No files found':
                payload['output_preview'] = self._truncate(output, 260)
        else:
            output = str(state.get('output') or '').strip()
            if output:
                payload['output_preview'] = self._truncate(output, 260)

        return AdapterEvent(kind='agent.tool_use', level=level, payload=payload)

    def _step_finish_event(self, obj: dict[str, Any], *, session_id: str | None) -> AdapterEvent:
        part = obj.get('part', {}) if isinstance(obj.get('part'), dict) else {}
        tokens = part.get('tokens', {}) if isinstance(part.get('tokens'), dict) else {}
        payload: dict[str, Any] = {
            'reason': part.get('reason'),
            'cost': part.get('cost'),
            'token_total': tokens.get('total'),
            'token_output': tokens.get('output'),
            'token_reasoning': tokens.get('reasoning'),
        }
        if session_id:
            payload['session_id'] = session_id
        return AdapterEvent(kind='agent.step_finish', payload=payload)

    @staticmethod
    def _truncate(value: str, limit: int) -> str:
        text = value.strip()
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + '...'
