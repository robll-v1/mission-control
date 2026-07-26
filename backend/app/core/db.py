from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from app.core.models import CheckRun, Event, Run, Task


class Database:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            '''
            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY,
              data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
              id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL,
              data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
              id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL,
              run_id TEXT,
              seq INTEGER NOT NULL,
              data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS check_runs (
              id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL,
              data TEXT NOT NULL
            );
            '''
        )
        self.conn.commit()

    def save_task(self, task: Task) -> None:
        self.conn.execute(
            'INSERT OR REPLACE INTO tasks (id, data) VALUES (?, ?)',
            (task.id, task.model_dump_json()),
        )
        self.conn.commit()

    def get_task(self, task_id: str) -> Task | None:
        row = self.conn.execute('SELECT data FROM tasks WHERE id = ?', (task_id,)).fetchone()
        return Task.model_validate_json(row['data']) if row else None

    def list_tasks(self) -> list[Task]:
        rows = self.conn.execute('SELECT data FROM tasks').fetchall()
        return [Task.model_validate_json(row['data']) for row in rows]

    def save_run(self, run: Run) -> None:
        self.conn.execute(
            'INSERT OR REPLACE INTO runs (id, task_id, data) VALUES (?, ?, ?)',
            (run.id, run.task_id, run.model_dump_json()),
        )
        self.conn.commit()

    def list_runs(self, task_id: str) -> list[Run]:
        """Return this task's runs oldest-first.

        Callers treat ``runs[-1]`` as the latest round, so the order must be
        explicit — a bare SELECT returns rows in unspecified order and only
        happens to come back in insert order today. Sort by ``round_index`` so
        the ordering is semantic, with insertion order as the tiebreak.
        """
        rows = self.conn.execute(
            'SELECT data FROM runs WHERE task_id = ? ORDER BY rowid ASC', (task_id,)
        ).fetchall()
        runs = [Run.model_validate_json(row['data']) for row in rows]
        # Stable sort: equal round_index keeps insertion order.
        runs.sort(key=lambda run: run.round_index)
        return runs

    def append_event(self, event: Event) -> None:
        self.conn.execute(
            'INSERT OR REPLACE INTO events (id, task_id, run_id, seq, data) VALUES (?, ?, ?, ?, ?)',
            (event.id, event.task_id, event.run_id, event.seq, event.model_dump_json()),
        )
        self.conn.commit()

    def list_events(self, task_id: str) -> list[Event]:
        rows = self.conn.execute(
            'SELECT data FROM events WHERE task_id = ? ORDER BY seq ASC', (task_id,)
        ).fetchall()
        return [Event.model_validate_json(row['data']) for row in rows]

    def save_check_run(self, check_run: CheckRun) -> None:
        self.conn.execute(
            'INSERT OR REPLACE INTO check_runs (id, task_id, data) VALUES (?, ?, ?)',
            (check_run.id, check_run.task_id, check_run.model_dump_json()),
        )
        self.conn.commit()

    def list_check_runs(self, task_id: str) -> list[CheckRun]:
        rows = self.conn.execute('SELECT data FROM check_runs WHERE task_id = ?', (task_id,)).fetchall()
        return [CheckRun.model_validate_json(row['data']) for row in rows]
