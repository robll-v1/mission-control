from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from app.core.models import CheckRun, Event, EventLevel, FindingStatus, Run, Task, TrackedFinding

SCHEMA_VERSION = 3
_SEVERITY_RANK = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
_CLOSED_STATUSES = {FindingStatus.RESOLVED, FindingStatus.DISMISSED}
_STATUS_RANK = {
    FindingStatus.NEW: 0,
    FindingStatus.PERSISTENT: 1,
    FindingStatus.UNVERIFIED: 2,
    FindingStatus.LIKELY_FIXED: 3,
    FindingStatus.RESOLVED: 4,
    FindingStatus.DISMISSED: 5,
}


class Database:
    """SQLite-backed store, safe to share across threads.

    One connection is shared by the FastAPI request threadpool, the SSE event
    generator, the review worker, and the validation worker.
    ``check_same_thread=False`` only disables Python's *ownership check* — it
    does not make concurrent use safe, and ``sqlite3.threadsafety == 3`` does
    not either: that covers the C library, while ``Connection.execute()`` is a
    Python-level convenience that creates a cursor and steps it as separate
    operations. Interleaving those across threads yields
    ``InterfaceError: bad parameter or other API misuse`` and rows returned with
    the wrong column count (``IndexError: tuple index out of range``).

    Every statement below therefore runs under ``_lock``, which also keeps each
    write paired with its ``commit()``. An ``RLock`` is used so a method may
    safely call another one in future without deadlocking.
    """

    def __init__(self, path: str):
        self.path = Path(path)
        self._is_memory = str(path) == ':memory:'
        if not self._is_memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            str(path) if self._is_memory else self.path,
            timeout=30.0,
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA busy_timeout = 30000')
        self._lock = threading.RLock()
        self.last_backup_path: Path | None = None
        self._init_schema()

    def close(self) -> None:
        """Close the underlying connection. Safe to call more than once."""
        with self._lock:
            self.conn.close()

    @contextmanager
    def locked(self):
        """Serialize a read-modify-write operation on this shared connection."""
        with self._lock:
            yield

    def schema_version(self) -> int:
        with self._lock:
            row = self.conn.execute('PRAGMA user_version').fetchone()
            return int(row[0]) if row else 0

    def generated_paths(self) -> list[Path]:
        """Return all on-disk SQLite files (main, WAL/journal/SHM, and migration backups)."""
        if self._is_memory:
            return []
        paths: list[Path] = [
            self.path,
            Path(f'{self.path}-journal'),
            Path(f'{self.path}-wal'),
            Path(f'{self.path}-shm'),
        ]
        if self.path.parent.exists():
            paths.extend(sorted(self.path.parent.glob(f'{self.path.name}.bak*')))
        if self.last_backup_path is not None and self.last_backup_path not in paths:
            paths.append(self.last_backup_path)
        return paths

    def _init_schema(self) -> None:
        with self._lock:
            self._init_schema_locked()

    def _backup_before_migration_locked(self, old_version: int) -> Path | None:
        """Create a consistent SQLite backup file before upgrading an existing DB."""
        if self._is_memory:
            return None
        if not self.path.exists() or self.path.stat().st_size == 0:
            return None
        stamp = time.strftime('%Y%m%d-%H%M%S', time.gmtime())
        candidate = self.path.with_name(f'{self.path.name}.bak.v{old_version}.{stamp}')
        suffix = 1
        while candidate.exists():
            candidate = self.path.with_name(f'{self.path.name}.bak.v{old_version}.{stamp}.{suffix}')
            suffix += 1
        dst = sqlite3.connect(candidate)
        try:
            self.conn.backup(dst)
        finally:
            dst.close()
        self.last_backup_path = candidate
        return candidate

    def _init_schema_locked(self) -> None:
        existing_tables = {
            row['name']
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        current_version = int(self.conn.execute('PRAGMA user_version').fetchone()[0])

        # Only snapshot a pre-migration backup when upgrading an existing DB
        # with prior tables (avoid creating clutter on brand-new databases).
        if existing_tables and current_version < SCHEMA_VERSION:
            self._backup_before_migration_locked(current_version)

        try:
            self._apply_schema_locked(current_version)
        except Exception:
            try:
                self.conn.rollback()
            except Exception:
                pass
            raise

    def _apply_schema_locked(self, current_version: int) -> None:
        # Base tables (v1) + first-class findings table (v2) are idempotent.
        self.conn.execute('BEGIN')
        self.conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY,
              data TEXT NOT NULL
            )
            '''
        )
        self.conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS runs (
              id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL,
              data TEXT NOT NULL
            )
            '''
        )
        self.conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS events (
              id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL,
              run_id TEXT,
              seq INTEGER NOT NULL,
              data TEXT NOT NULL
            )
            '''
        )
        self.conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS check_runs (
              id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL,
              data TEXT NOT NULL
            )
            '''
        )
        self.conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS findings (
              id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL,
              fingerprint TEXT NOT NULL,
              category TEXT NOT NULL DEFAULT '',
              severity TEXT NOT NULL,
              path TEXT,
              line INTEGER,
              title TEXT NOT NULL,
              detail TEXT NOT NULL DEFAULT '',
              first_round INTEGER NOT NULL,
              last_round INTEGER NOT NULL,
              status TEXT NOT NULL,
              system_status TEXT NOT NULL,
              user_status TEXT,
              location_changed INTEGER NOT NULL DEFAULT 0,
              reopened INTEGER NOT NULL DEFAULT 0,
              mentioned_after_dismiss INTEGER NOT NULL DEFAULT 0,
              previous_path TEXT,
              previous_line INTEGER,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              UNIQUE(task_id, fingerprint)
            )
            '''
        )
        self.conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_findings_task_id ON findings(task_id)'
        )
        self.conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS finding_sync_state (
              task_id TEXT PRIMARY KEY,
              last_synced_round INTEGER NOT NULL DEFAULT 0
            )
            '''
        )
        self.conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_events_task_seq ON events(task_id, seq)'
        )
        self.conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS task_event_seq (
              task_id TEXT PRIMARY KEY,
              last_seq INTEGER NOT NULL DEFAULT 0
            )
            '''
        )
        if current_version < SCHEMA_VERSION:
            self.conn.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
        self.conn.commit()

    def save_task(self, task: Task) -> None:
        with self._lock:
            self.conn.execute(
                'INSERT OR REPLACE INTO tasks (id, data) VALUES (?, ?)',
                (task.id, task.model_dump_json()),
            )
            self.conn.commit()

    def get_task(self, task_id: str) -> Task | None:
        with self._lock:
            row = self.conn.execute('SELECT data FROM tasks WHERE id = ?', (task_id,)).fetchone()
        return Task.model_validate_json(row['data']) if row else None

    def list_tasks(self) -> list[Task]:
        with self._lock:
            rows = self.conn.execute('SELECT data FROM tasks').fetchall()
        return [Task.model_validate_json(row['data']) for row in rows]

    def save_run(self, run: Run) -> None:
        with self._lock:
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
        with self._lock:
            rows = self.conn.execute(
                'SELECT data FROM runs WHERE task_id = ? ORDER BY rowid ASC', (task_id,)
            ).fetchall()
        runs = [Run.model_validate_json(row['data']) for row in rows]
        # Stable sort: equal round_index keeps insertion order.
        runs.sort(key=lambda run: run.round_index)
        return runs

    def _current_max_event_seq_locked(self, task_id: str) -> int:
        # Always query SQLite directly (indexed O(log N) lookup via task_event_seq
        # PK and idx_events_task_seq) so multiple connections / processes opening
        # the same DB file never read a stale in-memory sequence cache.
        seq_row = self.conn.execute(
            'SELECT last_seq FROM task_event_seq WHERE task_id = ?',
            (task_id,),
        ).fetchone()
        ev_row = self.conn.execute(
            'SELECT COALESCE(MAX(seq), 0) AS m FROM events WHERE task_id = ?',
            (task_id,),
        ).fetchone()
        seq_val = int(seq_row['last_seq']) if seq_row is not None else 0
        ev_val = int(ev_row['m']) if (ev_row is not None and ev_row['m'] is not None) else 0
        return max(seq_val, ev_val)

    def max_event_seq(self, task_id: str) -> int:
        with self._lock:
            return self._current_max_event_seq_locked(task_id)

    def append_event(self, event: Event) -> None:
        with self._lock:
            nested = self.conn.in_transaction
            if nested:
                self.conn.execute('SAVEPOINT amc_append_event')
            else:
                self.conn.execute('BEGIN IMMEDIATE')
            try:
                self.conn.execute(
                    'INSERT OR REPLACE INTO events (id, task_id, run_id, seq, data) VALUES (?, ?, ?, ?, ?)',
                    (event.id, event.task_id, event.run_id, event.seq, event.model_dump_json()),
                )
                self.conn.execute(
                    '''
                    INSERT INTO task_event_seq (task_id, last_seq) VALUES (?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET last_seq = MAX(last_seq, excluded.last_seq)
                    ''',
                    (event.task_id, int(event.seq)),
                )
                if nested:
                    self.conn.execute('RELEASE SAVEPOINT amc_append_event')
                self.conn.commit()
            except Exception:
                try:
                    if nested:
                        self.conn.execute('ROLLBACK TO SAVEPOINT amc_append_event')
                        self.conn.execute('RELEASE SAVEPOINT amc_append_event')
                    else:
                        self.conn.rollback()
                except Exception:
                    pass
                raise

    def append_event_auto_seq(
        self,
        *,
        task_id: str,
        kind: str,
        payload: dict,
        level: EventLevel = EventLevel.INFO,
        run_id: str | None = None,
        touch_task: bool = True,
        ts: float | None = None,
    ) -> Event:
        """Atomically allocate the next monotonic ``seq`` for ``task_id``, persist
        the event (and optionally bump ``task.updated_at``) in one ``BEGIN IMMEDIATE``
        transaction so even separate connections/processes sharing the same SQLite
        file can never allocate duplicate sequence numbers.
        """
        with self._lock:
            nested = self.conn.in_transaction
            if nested:
                self.conn.execute('SAVEPOINT amc_append_auto_event')
            else:
                self.conn.execute('BEGIN IMMEDIATE')
            try:
                next_seq = self._current_max_event_seq_locked(task_id) + 1
                event_ts = time.time() if ts is None else float(ts)
                event = Event(
                    task_id=task_id,
                    run_id=run_id,
                    seq=next_seq,
                    ts=event_ts,
                    kind=kind,
                    level=level,
                    payload=payload,
                )
                self.conn.execute(
                    'INSERT INTO events (id, task_id, run_id, seq, data) VALUES (?, ?, ?, ?, ?)',
                    (event.id, event.task_id, event.run_id, event.seq, event.model_dump_json()),
                )
                self.conn.execute(
                    '''
                    INSERT INTO task_event_seq (task_id, last_seq) VALUES (?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET last_seq = MAX(last_seq, excluded.last_seq)
                    ''',
                    (task_id, next_seq),
                )
                if touch_task:
                    row = self.conn.execute('SELECT data FROM tasks WHERE id = ?', (task_id,)).fetchone()
                    if row is not None:
                        task = Task.model_validate_json(row['data'])
                        task.updated_at = event.ts
                        self.conn.execute(
                            'INSERT OR REPLACE INTO tasks (id, data) VALUES (?, ?)',
                            (task.id, task.model_dump_json()),
                        )
                if nested:
                    self.conn.execute('RELEASE SAVEPOINT amc_append_auto_event')
                self.conn.commit()
                return event
            except Exception:
                try:
                    if nested:
                        self.conn.execute('ROLLBACK TO SAVEPOINT amc_append_auto_event')
                        self.conn.execute('RELEASE SAVEPOINT amc_append_auto_event')
                    else:
                        self.conn.rollback()
                except Exception:
                    pass
                raise

    def list_events(self, task_id: str) -> list[Event]:
        with self._lock:
            rows = self.conn.execute(
                'SELECT data FROM events WHERE task_id = ? ORDER BY seq ASC, rowid ASC', (task_id,)
            ).fetchall()
        return [Event.model_validate_json(row['data']) for row in rows]

    def list_events_after(
        self,
        task_id: str,
        *,
        after_seq: int = 0,
        limit: int | None = None,
    ) -> list[Event]:
        """Return events for ``task_id`` with ``seq > after_seq`` in ascending seq order."""
        with self._lock:
            if limit is not None and limit > 0:
                rows = self.conn.execute(
                    'SELECT data FROM events WHERE task_id = ? AND seq > ? ORDER BY seq ASC, rowid ASC LIMIT ?',
                    (task_id, int(after_seq), int(limit)),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    'SELECT data FROM events WHERE task_id = ? AND seq > ? ORDER BY seq ASC, rowid ASC',
                    (task_id, int(after_seq)),
                ).fetchall()
        return [Event.model_validate_json(row['data']) for row in rows]

    def save_check_run(self, check_run: CheckRun) -> None:
        with self._lock:
            self.conn.execute(
                'INSERT OR REPLACE INTO check_runs (id, task_id, data) VALUES (?, ?, ?)',
                (check_run.id, check_run.task_id, check_run.model_dump_json()),
            )
            self.conn.commit()

    def list_check_runs(self, task_id: str) -> list[CheckRun]:
        with self._lock:
            rows = self.conn.execute('SELECT data FROM check_runs WHERE task_id = ?', (task_id,)).fetchall()
        return [CheckRun.model_validate_json(row['data']) for row in rows]

    @staticmethod
    def _row_to_finding(row: sqlite3.Row) -> TrackedFinding:
        user_status = row['user_status']
        return TrackedFinding(
            id=row['id'],
            task_id=row['task_id'],
            fingerprint=row['fingerprint'],
            category=row['category'] or '',
            severity=row['severity'],
            path=row['path'],
            line=row['line'],
            title=row['title'],
            detail=row['detail'] or '',
            first_round=int(row['first_round']),
            last_round=int(row['last_round']),
            status=FindingStatus(row['status']),
            system_status=FindingStatus(row['system_status']),
            user_status=FindingStatus(user_status) if user_status else None,
            location_changed=bool(row['location_changed']),
            reopened=bool(row['reopened']),
            mentioned_after_dismiss=bool(row['mentioned_after_dismiss']),
            previous_path=row['previous_path'],
            previous_line=row['previous_line'],
            created_at=float(row['created_at']),
            updated_at=float(row['updated_at']),
        )

    @staticmethod
    def _finding_params(finding: TrackedFinding) -> tuple:
        return (
            finding.id,
            finding.task_id,
            finding.fingerprint,
            finding.category,
            finding.severity,
            finding.path,
            finding.line,
            finding.title,
            finding.detail,
            int(finding.first_round),
            int(finding.last_round),
            str(finding.status.value if isinstance(finding.status, FindingStatus) else finding.status),
            str(finding.system_status.value if isinstance(finding.system_status, FindingStatus) else finding.system_status),
            str(finding.user_status.value if isinstance(finding.user_status, FindingStatus) else finding.user_status)
            if finding.user_status is not None
            else None,
            1 if finding.location_changed else 0,
            1 if finding.reopened else 0,
            1 if finding.mentioned_after_dismiss else 0,
            finding.previous_path,
            finding.previous_line,
            float(finding.created_at),
            float(finding.updated_at),
        )

    def save_finding(self, finding: TrackedFinding) -> None:
        self.save_findings([finding])

    def get_synced_round(self, task_id: str) -> int:
        with self._lock:
            row = self.conn.execute(
                'SELECT last_synced_round FROM finding_sync_state WHERE task_id = ?',
                (task_id,),
            ).fetchone()
            if row is not None:
                return int(row['last_synced_round'])
            # Upgrade compatibility: if findings already exist for this task from
            # a pre-finding_sync_state schema, treat their MAX(last_round) as
            # already synced so re-reads never replay and overwrite user states.
            max_row = self.conn.execute(
                'SELECT MAX(last_round) AS m FROM findings WHERE task_id = ?',
                (task_id,),
            ).fetchone()
        return int(max_row['m']) if (max_row is not None and max_row['m'] is not None) else 0

    def set_synced_round(self, task_id: str, round_index: int) -> None:
        with self._lock:
            self.conn.execute(
                '''
                INSERT INTO finding_sync_state (task_id, last_synced_round) VALUES (?, ?)
                ON CONFLICT(task_id) DO UPDATE SET last_synced_round = MAX(last_synced_round, excluded.last_synced_round)
                ''',
                (task_id, int(round_index)),
            )
            self.conn.commit()

    def save_findings(
        self,
        findings: Iterable[TrackedFinding],
        *,
        task_id: str | None = None,
        synced_round: int | None = None,
    ) -> None:
        items = list(findings)
        if not items and synced_round is None:
            return
        sql = '''
            INSERT INTO findings (
              id, task_id, fingerprint, category, severity, path, line,
              title, detail, first_round, last_round, status, system_status,
              user_status, location_changed, reopened, mentioned_after_dismiss,
              previous_path, previous_line, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              fingerprint = excluded.fingerprint,
              category = excluded.category,
              severity = excluded.severity,
              path = excluded.path,
              line = excluded.line,
              title = excluded.title,
              detail = excluded.detail,
              first_round = excluded.first_round,
              last_round = excluded.last_round,
              status = excluded.status,
              system_status = excluded.system_status,
              user_status = excluded.user_status,
              location_changed = excluded.location_changed,
              reopened = excluded.reopened,
              mentioned_after_dismiss = excluded.mentioned_after_dismiss,
              previous_path = excluded.previous_path,
              previous_line = excluded.previous_line,
              updated_at = excluded.updated_at
        '''
        with self._lock:
            self.conn.execute('SAVEPOINT amc_save_findings')
            try:
                for item in items:
                    self.conn.execute(sql, self._finding_params(item))
                if synced_round is not None:
                    target_task = task_id or (items[0].task_id if items else None)
                    if target_task is not None:
                        self.conn.execute(
                            '''
                            INSERT INTO finding_sync_state (task_id, last_synced_round) VALUES (?, ?)
                            ON CONFLICT(task_id) DO UPDATE SET
                              last_synced_round = MAX(last_synced_round, excluded.last_synced_round)
                            ''',
                            (target_task, int(synced_round)),
                        )
                self.conn.execute('RELEASE SAVEPOINT amc_save_findings')
                self.conn.commit()
            except Exception:
                try:
                    self.conn.execute('ROLLBACK TO SAVEPOINT amc_save_findings')
                    self.conn.execute('RELEASE SAVEPOINT amc_save_findings')
                except Exception:
                    pass
                raise

    def get_finding(self, task_id: str, finding_id: str) -> TrackedFinding | None:
        with self._lock:
            row = self.conn.execute(
                'SELECT * FROM findings WHERE task_id = ? AND id = ?',
                (task_id, finding_id),
            ).fetchone()
        return self._row_to_finding(row) if row else None

    def list_findings(self, task_id: str) -> list[TrackedFinding]:
        with self._lock:
            rows = self.conn.execute(
                'SELECT * FROM findings WHERE task_id = ? ORDER BY created_at ASC, id ASC',
                (task_id,),
            ).fetchall()
        findings = [self._row_to_finding(row) for row in rows]
        findings.sort(
            key=lambda f: (
                1 if f.status in _CLOSED_STATUSES else 0,
                _SEVERITY_RANK.get(f.severity.lower(), 9),
                _STATUS_RANK.get(f.status, 9),
                f.first_round,
                f.created_at,
                f.id,
            )
        )
        return findings

    def delete_findings(self, task_id: str) -> None:
        with self._lock:
            self.conn.execute('DELETE FROM findings WHERE task_id = ?', (task_id,))
            self.conn.execute('DELETE FROM finding_sync_state WHERE task_id = ?', (task_id,))
            self.conn.commit()

    def delete_task(self, task_id: str) -> bool:
        """Atomically delete a task and all of its runs, events, checks, and findings."""
        with self._lock:
            in_outer_tx = self.conn.in_transaction
            self.conn.execute('SAVEPOINT amc_delete_task')
            try:
                cur = self.conn.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
                if cur.rowcount == 0:
                    self.conn.execute('RELEASE SAVEPOINT amc_delete_task')
                    return False
                self.conn.execute('DELETE FROM runs WHERE task_id = ?', (task_id,))
                self.conn.execute('DELETE FROM events WHERE task_id = ?', (task_id,))
                self.conn.execute('DELETE FROM check_runs WHERE task_id = ?', (task_id,))
                self.conn.execute('DELETE FROM findings WHERE task_id = ?', (task_id,))
                self.conn.execute('DELETE FROM finding_sync_state WHERE task_id = ?', (task_id,))
                self.conn.execute('DELETE FROM task_event_seq WHERE task_id = ?', (task_id,))
                self.conn.execute('RELEASE SAVEPOINT amc_delete_task')
                if not in_outer_tx:
                    self.conn.commit()
                return True
            except Exception:
                try:
                    self.conn.execute('ROLLBACK TO SAVEPOINT amc_delete_task')
                    self.conn.execute('RELEASE SAVEPOINT amc_delete_task')
                except Exception:
                    pass
                raise
