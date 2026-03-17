import asyncio
import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.core.mission_engine import MissionEngine


def get_engine() -> MissionEngine:
    from app.api.app import engine
    return engine


router = APIRouter(prefix='/api/tasks', tags=['stream'])


@router.get('/{task_id}/stream')
async def stream_task(task_id: str, mission: MissionEngine = Depends(get_engine)):
    async def event_generator():
        last_count = 0
        while True:
            events = mission.db.list_events(task_id)
            if len(events) > last_count:
                for event in events[last_count:]:
                    yield f"data: {json.dumps(event.model_dump())}\n\n"
                last_count = len(events)
            await asyncio.sleep(1)

    return StreamingResponse(event_generator(), media_type='text/event-stream')
