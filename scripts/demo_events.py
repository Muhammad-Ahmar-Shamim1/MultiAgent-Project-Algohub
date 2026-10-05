"""Emit demo events so the SSE feed can be watched with `curl -N`.

python scripts/demo_events.py --create          # create a project, print its id
curl -N localhost:8000/api/v1/projects/<id>/events
python scripts/demo_events.py <id>              # emit 3 events, 1 s apart
"""

import asyncio
import sys

from web_department.collaboration.events import EventType, close_event_bus, emit
from web_department.db.base import get_engine, session_scope
from web_department.db.repositories import ProjectRepository


async def main(arg: str) -> None:
    """Create a project (``--create``) or emit three events for an existing one."""
    if arg == "--create":
        async with session_scope() as session:
            project = await ProjectRepository(session).create(title="SSE demo", requester_id="demo")
        print(project.id)
    else:
        for event_type in (
            EventType.TASK_CREATED,
            EventType.TASK_STARTED,
            EventType.TASK_COMPLETED,
        ):
            event = await emit(event_type, project_id=arg, payload={"demo": True})
            print(f"emitted {event_type.value} {event.event_id}")
            await asyncio.sleep(1)
    await close_event_bus()
    await get_engine().dispose()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1]))
