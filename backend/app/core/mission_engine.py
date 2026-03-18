from __future__ import annotations

import time

from app.core.db import Database
from app.core.models import Event, EventLevel, Task, TaskStatus


class MissionEngine:
    def __init__(self, db: Database):
        self.db = db

    def create_task(
        self,
        *,
        title: str,
        repo_path: str,
        description: str = '',
        source_type: str = 'pull_request',
        source_url: str | None = None,
        backend: str = 'opencode',
        review_focus: str = '',
        pr_owner: str | None = None,
        pr_repo: str | None = None,
        pr_number: int | None = None,
        pr_head_ref: str | None = None,
        pr_head_sha: str | None = None,
        pr_base_ref: str | None = None,
        review_paths: list[str] | None = None,
    ) -> Task:
        now = time.time()
        task = Task(
            title=title,
            description=description,
            source_type=source_type,
            source_url=source_url,
            repo_path=repo_path,
            backend=backend,
            review_focus=review_focus,
            pr_owner=pr_owner,
            pr_repo=pr_repo,
            pr_number=pr_number,
            pr_head_ref=pr_head_ref,
            pr_head_sha=pr_head_sha,
            pr_base_ref=pr_base_ref,
            review_paths=review_paths or [],
            created_at=now,
            updated_at=now,
        )
        self.db.save_task(task)
        self.append_event(
            task_id=task.id,
            kind='task.created',
            payload={
                'title': task.title,
                'repo_path': task.repo_path,
                'source_url': task.source_url,
                'pr_number': task.pr_number,
            },
        )
        return task

    def set_stage(self, task_id: str, *, status: TaskStatus, stage: str) -> Task:
        task = self.db.get_task(task_id)
        if task is None:
            raise KeyError(f'task not found: {task_id}')
        task.status = status
        task.current_stage = stage
        task.updated_at = time.time()
        self.db.save_task(task)
        self.append_event(
            task_id=task.id,
            kind='task.stage_changed',
            payload={'status': task.status, 'stage': task.current_stage},
        )
        return task

    def append_event(
        self,
        *,
        task_id: str,
        kind: str,
        payload: dict,
        level: EventLevel = EventLevel.INFO,
        run_id: str | None = None,
        touch_task: bool = True,
    ) -> Event:
        seq = len(self.db.list_events(task_id)) + 1
        event = Event(
            task_id=task_id,
            run_id=run_id,
            seq=seq,
            ts=time.time(),
            kind=kind,
            level=level,
            payload=payload,
        )
        self.db.append_event(event)
        if touch_task:
            task = self.db.get_task(task_id)
            if task is not None:
                task.updated_at = event.ts
                self.db.save_task(task)
        return event
