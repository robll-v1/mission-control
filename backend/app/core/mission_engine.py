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
        source_type: str = 'manual',
        source_url: str | None = None,
        backend: str = 'opencode',
    ) -> Task:
        now = time.time()
        task = Task(
            title=title,
            description=description,
            source_type=source_type,
            source_url=source_url,
            repo_path=repo_path,
            backend=backend,
            created_at=now,
            updated_at=now,
        )
        self.db.save_task(task)
        self.append_event(
            task_id=task.id,
            kind='task.created',
            payload={'title': task.title, 'repo_path': task.repo_path},
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
