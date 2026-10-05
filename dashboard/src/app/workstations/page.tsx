"use client";

import { ProjectLink } from "@/components/common";
import { ListPage } from "@/components/list-page";
import { Badge } from "@/components/ui/badge";
import { api, unwrap, type Schemas } from "@/lib/api/client";
import { formatTime } from "@/lib/format";

export default function WorkstationsPage() {
  return (
    <ListPage<Schemas["WorkstationOut"]>
      title="Workstations"
      description="Isolated per-project containers the workers act in."
      queryKey="workstations"
      filters={[
        { key: "status", label: "Status", kind: "select", options: ["running", "stopped", "destroyed"] },
        { key: "project_id", label: "Project ID", kind: "text", placeholder: "prj_…" },
      ]}
      fetch={(f) =>
        unwrap(api.GET("/api/v1/workstations", { params: { query: { status: f.status, project_id: f.project_id } } }))
      }
      rowKey={(w) => w.id}
      columns={[
        { header: "Workstation", cell: (w) => <span className="font-mono text-xs">{w.id}</span> },
        { header: "Status", cell: (w) => <Badge tone={w.status === "running" ? "success" : "neutral"}>{w.status}</Badge> },
        { header: "Project", cell: (w) => <ProjectLink id={w.project_id} /> },
        { header: "Container", cell: (w) => <span className="font-mono text-xs">{w.container_id?.slice(0, 12) ?? "—"}</span> },
        {
          header: "Limits",
          cell: (w) =>
            Object.entries(w.limits)
              .map(([k, v]) => `${k}: ${String(v)}`)
              .join(", ") || "—",
        },
        { header: "Created", cell: (w) => formatTime(w.created_at) },
      ]}
      emptyText="No workstations match."
    />
  );
}
