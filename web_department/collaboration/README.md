# collaboration

Event bus (`events.py`: audit table + Redis Streams + SSE feed) and the War Room.

`war_room.py` (FR-080, FR-081):

- `open(session, registry, project_id)` forms the team from the plan. It recruits one agent per
  required role through the Agent Factory, and records a `WarRoom` plus its `WarRoomMember`
  rows. Required roles are every task's role, plus Security when the project has auth or
  sensitive data, plus DevOps when it has a deployment target. Members start AVAILABLE so the
  Task Router reuses them. Emits `WAR_ROOM_OPENED`.
- `view(session, project_id)` returns members, the task board by state, the latest events and
  the artifacts. This backs `GET /api/v1/projects/{id}/war-room`.
- `request_from(...)` is the only way one role asks another for something. It creates a READY
  task for that role (recruiting a member if needed), makes the asking task depend on it when
  `blocking`, and emits `REVIEW_REQUIRED`. There is no chat channel.
- `close(session, project_id)` retires every member (refusing with `WarRoomBusy` while one is
  WORKING), copies the artifacts to `$WAR_ROOM_ARCHIVE_DIR/<project_id>/<war_room_id>/`, and
  archives the room with the artifact manifest. Emits `WAR_ROOM_CLOSED`.
