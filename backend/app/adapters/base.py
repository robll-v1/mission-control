from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.core.models import Task


@dataclass
class AdapterEvent:
    kind: str
    level: str = 'info'
    payload: dict[str, Any] = field(default_factory=dict)


class RunnerAdapter(ABC):
    name: str

    @abstractmethod
    def make_command(self, *, task: Task, prompt: str) -> list[str]:
        raise NotImplementedError

    def parse_stdout_line(self, line: str) -> list[AdapterEvent]:
        stripped = line.strip()
        if not stripped:
            return []
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            return [AdapterEvent(kind='agent.text', payload={'text': stripped})]
        event_type = obj.get('type', 'event')
        payload = {'raw': obj}
        if 'sessionID' in obj:
            payload['session_id'] = obj['sessionID']
        return [AdapterEvent(kind=f'agent.{event_type}', payload=payload)]

    def parse_stderr_line(self, line: str) -> list[AdapterEvent]:
        stripped = line.rstrip()
        if not stripped:
            return []
        return [AdapterEvent(kind='agent.stderr', level='warning', payload={'text': stripped})]
