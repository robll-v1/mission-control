"""Concurrency regression tests for app.core.db.Database.

One connection is shared by the request threadpool, the SSE generator, and the
review/validation worker threads. Without serialization this raises
``sqlite3.InterfaceError: bad parameter or other API misuse`` or hands back rows
with the wrong column count. See issue #12.

Removing the ``_lock`` from ``Database`` makes ``test_concurrent_reads_and_writes``
fail reliably.
"""
from __future__ import annotations

import threading
import time

import pytest

from app.core.db import Database
from app.core.models import Event, EventLevel, Run, Task


def _task(**kw) -> Task:
    now = time.time()
    return Task(title=kw.pop('title', 't'), repo_path=kw.pop('repo_path', '.'),
                created_at=now, updated_at=now, **kw)


@pytest.fixture()
def db(tmp_path):
    database = Database(str(tmp_path / 'amc.db'))
    yield database
    database.close()


def test_concurrent_reads_and_writes(db):
    """Hammer the shared connection from many threads at once."""
    tasks = []
    for i in range(4):
        task = _task(title=f'task {i}')
        db.save_task(task)
        tasks.append(task)
        for r in range(3):
            db.save_run(Run(task_id=task.id, backend='opencode', round_index=r + 1,
                            status='completed', started_at=time.time()))
        for s in range(12):
            db.append_event(Event(task_id=task.id, seq=s + 1, ts=time.time(),
                                  kind='agent.text', level=EventLevel.INFO,
                                  payload={'text': 'x' * 120}))

    errors: list[BaseException] = []
    stop = threading.Event()
    barrier = threading.Barrier(24)

    def worker(fn):
        barrier.wait()
        while not stop.is_set():
            try:
                fn()
            except BaseException as exc:  # noqa: BLE001 - record and stop
                errors.append(exc)
                return

    threads: list[threading.Thread] = []
    for i in range(4):
        task = tasks[i]
        threads.append(threading.Thread(target=worker, args=(lambda t=task: db.list_runs(t.id),), daemon=True))
        threads.append(threading.Thread(target=worker, args=(lambda t=task: db.list_events(t.id),), daemon=True))
        threads.append(threading.Thread(target=worker, args=(db.list_tasks,), daemon=True))
        threads.append(threading.Thread(target=worker, args=(lambda t=task: db.get_task(t.id),), daemon=True))
        threads.append(threading.Thread(
            target=worker,
            args=(lambda t=task: db.append_event(Event(task_id=t.id, seq=99, ts=time.time(),
                                                       kind='agent.text', payload={'text': 'y'})),),
            daemon=True))
        threads.append(threading.Thread(
            target=worker,
            args=(lambda t=task: db.save_task(t),),
            daemon=True))

    for t in threads:
        t.start()
    time.sleep(2.5)
    stop.set()
    for t in threads:
        t.join(timeout=5)

    assert not errors, f'{len(errors)} concurrent access failure(s), first: {errors[0]!r}'


def test_rows_keep_their_shape_under_concurrency(db):
    """Reads must never return a row that fails to deserialize.

    The nastier face of this bug is not an exception from sqlite but a row with
    a mismatched column count, which surfaces later as a pydantic/IndexError far
    from the cause.
    """
    task = _task(title='shape')
    db.save_task(task)
    for s in range(40):
        db.append_event(Event(task_id=task.id, seq=s + 1, ts=time.time(),
                              kind='agent.text', payload={'text': 'z' * 200}))

    seen: list[int] = []
    errors: list[BaseException] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                seen.append(len(db.list_events(task.id)))
                seen.append(len(db.list_tasks()))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                return

    def writer():
        n = 100
        while not stop.is_set():
            try:
                n += 1
                db.append_event(Event(task_id=task.id, seq=n, ts=time.time(),
                                      kind='agent.tool_use', payload={'tool': 'read'}))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                return

    threads = [threading.Thread(target=reader, daemon=True) for _ in range(8)]
    threads += [threading.Thread(target=writer, daemon=True) for _ in range(4)]
    for t in threads:
        t.start()
    time.sleep(2.0)
    stop.set()
    for t in threads:
        t.join(timeout=5)

    assert not errors, f'first failure: {errors[0]!r}'
    assert seen, 'readers never completed a query'
    assert all(n >= 0 for n in seen)


def test_close_is_idempotent(tmp_path):
    database = Database(str(tmp_path / 'amc.db'))
    database.close()
    database.close()
