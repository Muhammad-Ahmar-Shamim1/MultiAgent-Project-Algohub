"use client";

import * as React from "react";

import { Badge } from "@/components/ui/badge";
import { eventStreamUrl } from "@/lib/api/client";
import { formatTime, humanize, stateTone } from "@/lib/format";

type FeedEvent = {
  event_id: string;
  event: string;
  status?: string | null;
  task_id?: string | null;
  agent_id?: string | null;
  payload?: Record<string, unknown>;
  timestamp: string;
};

const MAX_EVENTS = 200;

function summary(e: FeedEvent): string {
  const payload = e.payload ?? {};
  if (e.event === "STATE_CHANGED" && "to" in payload) {
    return `${String(payload.object ?? "")} → ${humanize(String(payload.to))}`;
  }
  for (const key of ["summary", "reason", "title", "message", "command"]) {
    if (typeof payload[key] === "string") return payload[key] as string;
  }
  return "";
}

/** Live project events over SSE (IR-003): replays history, then updates without reload.
 * EventSource reconnects by itself and resends ``Last-Event-ID``, so nothing is missed. */
export function EventFeed({ projectId, onEvent }: { projectId: string; onEvent?: () => void }) {
  const [events, setEvents] = React.useState<FeedEvent[]>([]);
  const [connected, setConnected] = React.useState(false);
  const onEventRef = React.useRef(onEvent);
  onEventRef.current = onEvent;

  React.useEffect(() => {
    const source = new EventSource(eventStreamUrl(projectId));
    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);
    source.onmessage = () => undefined;
    const handler = (message: MessageEvent<string>) => {
      try {
        const parsed = JSON.parse(message.data) as FeedEvent;
        setEvents((current) =>
          current.some((e) => e.event_id === parsed.event_id)
            ? current
            : [parsed, ...current].slice(0, MAX_EVENTS),
        );
        onEventRef.current?.();
      } catch {
        // ignore a malformed frame rather than dropping the stream
      }
    };
    // The API names every SSE frame after its event type, so listen to each type it sends.
    const types = [
      "PROJECT_RECEIVED", "STATE_CHANGED", "TASK_CREATED", "TASK_ASSIGNED", "TASK_STARTED",
      "DEPENDENCY_WAITING", "TASK_COMPLETED", "REVIEW_REQUIRED", "TEST_FAILED", "FIX_REQUIRED",
      "SECURITY_BLOCKED", "APPROVAL_REQUESTED", "APPROVED", "REJECTED", "DEPLOYED", "ROLLED_BACK",
      "ALERT_RAISED", "TOOL_CALL", "WAR_ROOM_OPENED", "WAR_ROOM_CLOSED",
    ];
    for (const type of types) source.addEventListener(type, handler as EventListener);
    return () => source.close();
  }, [projectId]);

  return (
    <div>
      <p className="mb-2 text-xs text-muted-foreground" aria-live="polite">
        {connected ? "Live" : "Connecting…"} · {events.length} event(s)
      </p>
      <ol aria-live="polite" aria-relevant="additions" className="max-h-96 space-y-2 overflow-y-auto">
        {events.map((e) => (
          <li key={e.event_id} className="rounded-md border p-2 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={stateTone(e.event.replace(/^TASK_/, ""))}>{humanize(e.event)}</Badge>
              <time className="text-xs text-muted-foreground" dateTime={e.timestamp}>
                {formatTime(e.timestamp)}
              </time>
            </div>
            {summary(e) ? <p className="mt-1 break-words">{summary(e)}</p> : null}
          </li>
        ))}
      </ol>
    </div>
  );
}
