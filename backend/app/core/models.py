from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class TaskStatus(StrEnum):
    CREATED = 'created'
    INGESTING = 'ingesting'
    CONTEXT_READY = 'context_ready'
    RUNNING = 'running'
    WAITING_HUMAN = 'waiting_human'
    VALIDATING = 'validating'
    COMPLETED = 'completed'
    FAILED = 'failed'
    ABORTED = 'aborted'


class EventLevel(StrEnum):
    INFO = 'info'
    WARNING = 'warning'
    ERROR = 'error'


class Task(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    title: str
    description: str = ''
    source_type: str = 'manual'
    source_url: str | None = None
    repo_path: str
    backend: str = 'opencode'
    status: TaskStatus = TaskStatus.CREATED
    current_stage: str = 'created'
    branch_name: str | None = None
    worktree_path: str | None = None
    created_at: float
    updated_at: float
    last_run_id: str | None = None


class Run(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    task_id: str
    backend: str
    backend_session_id: str | None = None
    status: str = 'created'
    started_at: float
    ended_at: float | None = None
    exit_code: int | None = None


class Event(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    task_id: str
    run_id: str | None = None
    seq: int
    ts: float
    kind: str
    level: EventLevel = EventLevel.INFO
    payload: dict[str, Any] = Field(default_factory=dict)


class Artifact(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    task_id: str
    kind: str
    path: str
    meta: dict[str, Any] = Field(default_factory=dict)


class CheckRun(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    task_id: str
    name: str
    command: str
    status: str
    exit_code: int | None = None
    duration_sec: float | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
