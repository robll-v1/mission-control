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


class ReviewVerdict(StrEnum):
    CLEAR = 'clear'
    CONCERNS = 'concerns'
    FAILED = 'failed'
    INCONCLUSIVE = 'inconclusive'


class ReviewFinding(BaseModel):
    severity: str
    summary: str
    path: str | None = None
    line: int | None = None
    detail: str = ''


class ReviewResult(BaseModel):
    verdict: ReviewVerdict = ReviewVerdict.INCONCLUSIVE
    summary: str = ''
    findings: list[ReviewFinding] = Field(default_factory=list)
    severity_counts: dict[str, int] = Field(default_factory=dict)
    finding_count: int = 0
    source_event_seq: int | None = None
    supersedes_round_index: int | None = None


class Task(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    title: str
    description: str = ''
    source_type: str = 'pull_request'
    source_url: str | None = None
    repo_path: str
    backend: str = 'opencode'
    review_focus: str = ''
    pr_owner: str | None = None
    pr_repo: str | None = None
    pr_number: int | None = None
    pr_head_ref: str | None = None
    pr_head_sha: str | None = None
    pr_base_ref: str | None = None
    review_paths: list[str] = Field(default_factory=list)
    status: TaskStatus = TaskStatus.CREATED
    current_stage: str = 'created'
    branch_name: str | None = None
    worktree_path: str | None = None
    latest_review_result: ReviewResult | None = None
    created_at: float
    updated_at: float
    last_run_id: str | None = None


class Run(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    task_id: str
    backend: str
    round_index: int = 1
    review_note: str = ''
    review_revision: str | None = None
    backend_session_id: str | None = None
    status: str = 'created'
    review_result: ReviewResult | None = None
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
