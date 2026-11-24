"""P4 EventBus, persistent monotonic event seq, and SSE Last-Event-ID tests."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes_stream
from app.core.db import SCHEMA_VERSION, Database
from app.core.event_bus import EventBus
from app.core.mission_engine import MissionEngine
from app.core.models import Event, EventLevel, Task


def _parse_sse_frames(chunks: list[str]) -> list[tuple[int, dict]]:
    """Parse SSE text chunks into ``(id, json_data)`` tuples."""
    buf = ''.join(chunks)
    frames: list[tuple[int, dict]] = []
    for raw_block in buf.split('\n\n'):
        block = raw_block.strip()
        if not block or block.startswith(':'):
            continue
        ev_id: int | None = None
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith('id:'):
                ev_id = int(line[3:].strip())
            elif line.startswith('data:'):
                data_lines.append(line[5:].strip())
        if ev_id is not None and data_lines:
            frames.append((ev_id, json.loads('\n'.join(data_lines))))
    return frames


# ---------------------------------------------------------------------------
# 1. Persistent monotonic seq across restarts & concurrent writers
# ---------------------------------------------------------------------------


def test_event_seq_persists_monotonically_across_restarts_and_direct_inserts(tmp_path):
    db_file = tmp_path / 'seq.db'
    db1 = Database(str(db_file))
    mission1 = MissionEngine(db1)
    task = mission1.create_task(title='seq-test', repo_path=str(tmp_path))
    # task.created is seq 1
    e2 = mission1.append_event(task_id=task.id, kind='agent.text', payload={'text': 'a'})
    e3 = mission1.append_event(task_id=task.id, kind='agent.text', payload={'text': 'b'})
    assert [e2.seq, e3.seq] == [2, 3]
    db1.close()

    # Reopen after simulated crash/restart: seq MUST resume at 4, never reset to 1
    db2 = Database(str(db_file))
    mission2 = MissionEngine(db2)
    try:
        e4 = mission2.append_event(task_id=task.id, kind='agent.text', payload={'text': 'c'})
        assert e4.seq == 4
        assert db2.max_event_seq(task.id) == 4

        # Direct db.append_event with a higher seq advances the counter too
        db2.append_event(Event(task_id=task.id, seq=50, ts=time.time(), kind='custom', payload={}))
        e51 = mission2.append_event(task_id=task.id, kind='agent.text', payload={'text': 'd'})
        assert e51.seq == 51
        after_3 = db2.list_events_after(task.id, after_seq=3)
        assert [e.seq for e in after_3] == [4, 50, 51]
    finally:
        db2.close()


def test_concurrent_append_event_produces_strictly_contiguous_unique_seqs(tmp_path):
    db = Database(str(tmp_path / 'concurrent.db'))
    mission = MissionEngine(db)
    task = mission.create_task(title='stress', repo_path=str(tmp_path))  # seq 1

    thread_count = 12
    per_thread = 35
    barrier = threading.Barrier(thread_count)
    errors: list[BaseException] = []
    allocated: list[int] = []
    alloc_lock = threading.Lock()

    def writer(idx: int):
        try:
            barrier.wait()
            for n in range(per_thread):
                ev = mission.append_event(
                    task_id=task.id,
                    kind='agent.text',
                    payload={'worker': idx, 'n': n},
                    touch_task=False,
                )
                with alloc_lock:
                    allocated.append(ev.seq)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    try:
        assert not errors, errors
        expected_total = 1 + thread_count * per_thread
        assert sorted(allocated) == list(range(2, expected_total + 1))
        stored = db.list_events(task.id)
        assert [e.seq for e in stored] == list(range(1, expected_total + 1))
        assert db.max_event_seq(task.id) == expected_total
    finally:
        db.close()


def test_schema_v2_to_v3_migration_creates_backup_and_event_seq_table(tmp_path):
    db_file = tmp_path / 'v2.db'
    conn = sqlite3.connect(db_file)
    conn.executescript(
        '''
        CREATE TABLE tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE runs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, data TEXT NOT NULL);
        CREATE TABLE events (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, run_id TEXT, seq INTEGER NOT NULL, data TEXT NOT NULL);
        CREATE TABLE check_runs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, data TEXT NOT NULL);
        PRAGMA user_version = 2;
        '''
    )
    t = Task(id='t_v2', title='v2 task', repo_path=str(tmp_path), created_at=1.0, updated_at=1.0)
    ev = Event(id='e10', task_id=t.id, seq=10, ts=1.0, kind='task.created', payload={})
    conn.execute('INSERT INTO tasks (id, data) VALUES (?, ?)', (t.id, t.model_dump_json()))
    conn.execute(
        'INSERT INTO events (id, task_id, run_id, seq, data) VALUES (?, ?, ?, ?, ?)',
        (ev.id, ev.task_id, ev.run_id, ev.seq, ev.model_dump_json()),
    )
    conn.commit()
    conn.close()

    db = Database(str(db_file))
    mission = MissionEngine(db)
    try:
        assert db.schema_version() == SCHEMA_VERSION
        assert db.last_backup_path is not None and '.bak.v2.' in db.last_backup_path.name
        # Next event recovers MAX(events.seq)=10 -> allocates 11
        e11 = mission.append_event(task_id='t_v2', kind='agent.text', payload={'text': 'after upgrade'})
        assert e11.seq == 11
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 2. SSE Last-Event-ID, boundary-window dedup, slow-consumer resync, shutdown
# ---------------------------------------------------------------------------


def _build_stream_app(mission: MissionEngine) -> FastAPI:
    app = FastAPI()
    app.include_router(routes_stream.router)
    app.dependency_overrides[routes_stream.get_engine] = lambda: mission
    return app


def test_sse_404_for_missing_task(tmp_path):
    db = Database(str(tmp_path / 'sse404.db'))
    mission = MissionEngine(db)
    client = TestClient(_build_stream_app(mission))
    try:
        res = client.get('/api/tasks/does-not-exist/stream')
        assert res.status_code == 404
    finally:
        db.close()


def test_sse_last_event_id_resumes_without_duplicates_and_handles_malformed_ids(tmp_path):
    db = Database(str(tmp_path / 'sse_resume.db'))
    mission = MissionEngine(db)
    task = mission.create_task(title='sse', repo_path=str(tmp_path))  # seq 1
    for i in range(2, 6):
        mission.append_event(task_id=task.id, kind='agent.text', payload={'n': i})

    # Close bus shortly after backlog drains so TestClient stream finishes cleanly
    async def collect(headers=None, query=''):
        app = _build_stream_app(mission)

        class _FakeRequest:
            async def is_disconnected(self):
                return False

        # Invoke endpoint coroutine directly on this event loop so we can control shutdown deterministically.
        mission.event_bus.reopen()
        resp = await routes_stream.stream_task(
            task_id=task.id,
            request=_FakeRequest(),
            last_event_id=(headers or {}).get('Last-Event-ID'),
            since_seq=query or None,
            last_event_id_query=None,
            mission=mission,
        )
        chunks: list[str] = []

        async def close_soon():
            await asyncio.sleep(0.05)
            mission.event_bus.close_all()

        closer = asyncio.create_task(close_soon())
        async for chunk in resp.body_iterator:
            chunks.append(chunk if isinstance(chunk, str) else chunk.decode('utf-8'))
        await closer
        return _parse_sse_frames(chunks)

    try:
        # Resume after seq 3 -> receives ONLY 4 and 5
        frames = asyncio.run(collect(headers={'Last-Event-ID': '3'}))
        assert [fid for fid, _ in frames] == [4, 5]
        assert [data['seq'] for _, data in frames] == [4, 5]

        # Malformed Last-Event-ID gracefully defaults to 0 -> receives 1..5
        frames_bad = asyncio.run(collect(headers={'Last-Event-ID': 'not-a-number'}))
        assert [fid for fid, _ in frames_bad] == [1, 2, 3, 4, 5]

        # Query param since_seq=4 -> receives ONLY 5
        frames_q = asyncio.run(collect(query='4'))
        assert [fid for fid, _ in frames_q] == [5]
        assert mission.event_bus.subscriber_count(task.id) == 0
    finally:
        db.close()


def test_sse_slow_consumer_bounded_queue_resyncs_from_db_without_drops_or_dups(tmp_path):
    """Tiny queue (maxsize=4) bombarded with 60 concurrent events from a worker
    thread must never block the publisher and must deliver every seq 1..61 in
    strict order with zero gaps and zero duplicates via SQLite resync.
    """
    db = Database(str(tmp_path / 'slow.db'))
    bus = EventBus(max_queue_size=4)
    mission = MissionEngine(db, event_bus=bus)
    task = mission.create_task(title='slow-consumer', repo_path=str(tmp_path))  # seq 1

    async def run_scenario():
        class _FakeRequest:
            async def is_disconnected(self):
                return False

        resp = await routes_stream.stream_task(
            task_id=task.id,
            request=_FakeRequest(),
            last_event_id=None,
            since_seq=None,
            last_event_id_query=None,
            mission=mission,
        )

        # Start publishing 60 events from another OS thread BEFORE the consumer
        # starts iterating, overflowing the maxsize=4 queue and exercising the
        # boundary window between DB backlog and live queue simultaneously!
        def burst():
            for n in range(2, 62):
                mission.append_event(
                    task_id=task.id,
                    kind='agent.text',
                    payload={'n': n},
                    touch_task=False,
                )

        pub_thread = threading.Thread(target=burst)
        pub_thread.start()
        pub_thread.join(timeout=10)
        assert not pub_thread.is_alive(), 'publisher blocked on slow consumer!'

        # Publish 10 more live events while consuming, then close cleanly
        chunks: list[str] = []
        gen = resp.body_iterator

        async def read_all():
            async for chunk in gen:
                chunks.append(chunk if isinstance(chunk, str) else chunk.decode('utf-8'))

        reader = asyncio.create_task(read_all())
        await asyncio.sleep(0.05)

        def live_burst():
            for n in range(62, 72):
                mission.append_event(
                    task_id=task.id,
                    kind='agent.text',
                    payload={'n': n},
                    touch_task=False,
                )

        t2 = threading.Thread(target=live_burst)
        t2.start()
        t2.join(timeout=10)
        await asyncio.sleep(0.08)
        bus.close_all()
        await asyncio.wait_for(reader, timeout=5.0)
        return _parse_sse_frames(chunks)

    try:
        frames = asyncio.run(run_scenario())
        seqs = [fid for fid, _ in frames]
        assert seqs == list(range(1, 72))
        assert [data['seq'] for _, data in frames] == list(range(1, 72))
        assert bus.subscriber_count(task.id) == 0
    finally:
        db.close()


def test_two_separate_connections_on_same_db_allocate_unique_monotonic_seqs(tmp_path):
    """Two Database instances (e.g. API + SDK) sharing one SQLite file must never
    allocate duplicate seq numbers, and a manual low-seq insert on a cold
    connection must never regress the next auto-allocated seq.
    """
    db_file = tmp_path / 'shared.db'
    db_a = Database(str(db_file))
    db_b = Database(str(db_file))
    mission_a = MissionEngine(db_a)
    mission_b = MissionEngine(db_b)
    task = mission_a.create_task(title='shared', repo_path=str(tmp_path))  # seq 1

    try:
        e2 = mission_a.append_event(task_id=task.id, kind='agent.text', payload={'from': 'a'})
        e3 = mission_b.append_event(task_id=task.id, kind='agent.text', payload={'from': 'b'})
        e4 = mission_a.append_event(task_id=task.id, kind='agent.text', payload={'from': 'a2'})
        assert [e2.seq, e3.seq, e4.seq] == [2, 3, 4]

        # Fresh connection manually writing a lower seq must never regress auto-seq
        db_c = Database(str(db_file))
        try:
            db_c.append_event(Event(task_id=task.id, seq=2, ts=time.time(), kind='manual', payload={}))
            e5 = MissionEngine(db_c).append_event(task_id=task.id, kind='agent.text', payload={'from': 'c'})
            assert e5.seq == 5
        finally:
            db_c.close()
    finally:
        db_a.close()
        db_b.close()


def test_event_bus_coalesces_callbacks_to_one_per_subscriber_when_loop_paused():
    """Publishing 400 events while the event loop is paused must schedule at most
    ONE pending wake callback per subscriber (O(1) callback memory).
    """
    class _RecordingLoop:
        def __init__(self):
            self.callbacks: list = []

        def is_closed(self) -> bool:
            return False

        def call_soon_threadsafe(self, fn, *args):
            self.callbacks.append((fn, args))

    loop = _RecordingLoop()
    bus = EventBus(max_queue_size=1)

    async def _make_subs():
        # Create subscriptions while an asyncio loop exists for asyncio.Event(),
        # then swap their target loop to _RecordingLoop to count scheduled wakes.
        s1 = bus.subscribe('t1')
        s2 = bus.subscribe('t1')
        s1.loop = loop  # type: ignore[assignment]
        s2.loop = loop  # type: ignore[assignment]
        return s1, s2

    s1, s2 = asyncio.run(_make_subs())
    for seq in range(1, 421):
        bus.publish(Event(task_id='t1', seq=seq, ts=float(seq), kind='agent.text', payload={}))

    # Strictly at most 1 pending wake callback per subscriber (2 total, not 840!)
    assert len(loop.callbacks) == 2
    assert s1.overflowed is True and s2.overflowed is True
    bus.close_all()


def test_sse_disconnect_before_iteration_does_not_leak_subscriber(tmp_path):
    """If the ASGI client disconnects before StreamingResponse iterates the
    generator, zero subscribers must remain registered on EventBus.
    """
    db = Database(str(tmp_path / 'leak.db'))
    mission = MissionEngine(db)
    task = mission.create_task(title='disconnect', repo_path=str(tmp_path))

    async def run():
        resp = await routes_stream.stream_task(
            task_id=task.id,
            request=None,  # type: ignore[arg-type]
            last_event_id=None,
            since_seq=None,
            last_event_id_query=None,
            mission=mission,
        )

        async def receive():
            return {'type': 'http.disconnect'}

        async def send(_message):
            await asyncio.Future()

        await asyncio.wait_for(
            resp({'type': 'http', 'asgi': {'spec_version': '2.3'}}, receive, send),
            timeout=1.0,
        )
        await resp.body_iterator.aclose()
        assert mission.event_bus.subscriber_count(task.id) == 0

    try:
        asyncio.run(run())
    finally:
        mission.event_bus.close_all()
        db.close()
