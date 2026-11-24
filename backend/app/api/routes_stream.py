from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.core.mission_engine import MissionEngine
from app.core.models import Event

HEARTBEAT_INTERVAL_SEC = 15.0


def get_engine() -> MissionEngine:
    from app.api.app import engine
    return engine


def _parse_seq(raw: str | int | None) -> int:
    if raw is None or isinstance(raw, bool):
        return 0
    if isinstance(raw, int):
        return raw if raw > 0 else 0
    cleaned = str(raw).strip()
    if not cleaned or not cleaned.isascii() or not cleaned.isdigit():
        return 0
    stripped = cleaned.lstrip('0')
    if not stripped or len(stripped) > 18:
        return 0
    return int(stripped)


def _format_sse_event(event: Event) -> str:
    # Include `id: <seq>` so browser EventSource sends `Last-Event-ID: <seq>`
    # automatically on reconnect. Keep the default SSE message event type (no
    # custom `event:` line) so `EventSource.onmessage` continues to receive
    # every event payload cleanly.
    return f'id: {event.seq}\ndata: {json.dumps(event.model_dump())}\n\n'


router = APIRouter(prefix='/api/tasks', tags=['stream'])


@router.get('/{task_id}/stream')
async def stream_task(
    task_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias='Last-Event-ID'),
    since_seq: str | None = Query(default=None),
    last_event_id_query: str | None = Query(default=None, alias='last_event_id'),
    mission: MissionEngine = Depends(get_engine),
):
    task = await asyncio.to_thread(mission.db.get_task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail='task not found')

    # Prefer standard Last-Event-ID header (sent automatically on reconnect),
    # falling back to explicit query parameters for initial resume.
    initial_cursor = max(
        _parse_seq(last_event_id),
        _parse_seq(last_event_id_query),
        _parse_seq(since_seq),
    )

    # Subscribe BEFORE reading DB backlog so events published during the
    # boundary window are queued and deduplicated by `seq` instead of lost.
    loop = asyncio.get_running_loop()
    sub = mission.event_bus.subscribe(task_id, loop=loop)

    async def event_generator():
        last_sent_seq = initial_cursor
        try:
            backlog = await asyncio.to_thread(
                mission.db.list_events_after, task_id, after_seq=last_sent_seq
            )
            for ev in backlog:
                if ev.seq <= last_sent_seq:
                    continue
                yield _format_sse_event(ev)
                last_sent_seq = ev.seq

            while True:
                if await request.is_disconnected():
                    break

                # If the bounded queue overflowed while catching up or stalling,
                # resync directly from SQLite so no events are ever dropped.
                if sub.consume_overflow():
                    resync = await asyncio.to_thread(
                        mission.db.list_events_after, task_id, after_seq=last_sent_seq
                    )
                    for ev in resync:
                        if ev.seq <= last_sent_seq:
                            continue
                        yield _format_sse_event(ev)
                        last_sent_seq = ev.seq
                    continue

                try:
                    event = await asyncio.wait_for(sub.get(), timeout=HEARTBEAT_INTERVAL_SEC)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ': keepalive\n\n'
                    continue

                if event is None:
                    # EventBus shutdown sentinel.
                    break

                if sub.consume_overflow():
                    resync = await asyncio.to_thread(
                        mission.db.list_events_after, task_id, after_seq=last_sent_seq
                    )
                    for ev in resync:
                        if ev.seq <= last_sent_seq:
                            continue
                        yield _format_sse_event(ev)
                        last_sent_seq = ev.seq
                    continue

                if event.seq <= last_sent_seq:
                    # Already sent during backlog catch-up or earlier resync.
                    continue

                if event.seq > last_sent_seq + 1:
                    # Fill any gap (e.g. out-of-order thread scheduling) from DB
                    # before emitting `event` so seq stays strictly ordered.
                    gap_events = await asyncio.to_thread(
                        mission.db.list_events_after, task_id, after_seq=last_sent_seq
                    )
                    for ev in gap_events:
                        if ev.seq <= last_sent_seq:
                            continue
                        yield _format_sse_event(ev)
                        last_sent_seq = ev.seq
                    continue

                yield _format_sse_event(event)
                last_sent_seq = event.seq
        finally:
            mission.event_bus.unsubscribe(sub)

    return StreamingResponse(
        event_generator(),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        },
    )
