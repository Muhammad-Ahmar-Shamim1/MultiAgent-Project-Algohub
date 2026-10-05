"use client";

import { ProjectLink } from "@/components/common";
import { ListPage } from "@/components/list-page";
import { Badge } from "@/components/ui/badge";
import { api, unwrap, type Schemas } from "@/lib/api/client";
import { formatTime, humanize } from "@/lib/format";
import { ALERT_EVENTS } from "@/lib/states";

function detail(payload: Record<string, unknown>): string {
  for (const key of ["reason", "failure_summary", "reasons", "summary"]) {
    const value = payload[key];
    if (typeof value === "string") return value;
    if (Array.isArray(value)) return value.join("; ");
  }
  return "";
}

export default function MonitoringPage() {
  return (
    <ListPage<Schemas["EventOut"]>
      title="Monitoring"
      description="Alerts, security blocks, failed tests and rollbacks, newest first."
      queryKey="alerts"
      filters={[
        { key: "event", label: "Kind", kind: "select", options: ALERT_EVENTS },
        { key: "project_id", label: "Project ID", kind: "text", placeholder: "prj_…" },
      ]}
      fetch={(f) =>
        unwrap(api.GET("/api/v1/monitoring/alerts", { params: { query: { event: f.event, project_id: f.project_id } } }))
      }
      rowKey={(e) => e.event_id}
      columns={[
        { header: "When", cell: (e) => formatTime(e.timestamp) },
        { header: "Kind", cell: (e) => <Badge tone="danger">{humanize(e.event)}</Badge> },
        { header: "Detail", cell: (e) => <span className="block max-w-lg whitespace-normal">{detail(e.payload)}</span> },
        { header: "Project", cell: (e) => <ProjectLink id={e.project_id} /> },
      ]}
      emptyText="No alerts. All quiet."
    />
  );
}
