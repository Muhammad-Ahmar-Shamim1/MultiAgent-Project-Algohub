"use client";

import { ProjectLink } from "@/components/common";
import { ListPage } from "@/components/list-page";
import { Badge } from "@/components/ui/badge";
import { api, unwrap, type Schemas } from "@/lib/api/client";
import { formatTime } from "@/lib/format";

export default function DeploymentsPage() {
  return (
    <ListPage<Schemas["DeploymentOut"]>
      title="Deployments"
      description="Approved builds that were shipped, with their health."
      queryKey="deployments"
      filters={[
        { key: "status", label: "Status", kind: "text", placeholder: "e.g. deployed" },
        { key: "project_id", label: "Project ID", kind: "text", placeholder: "prj_…" },
      ]}
      fetch={(f) =>
        unwrap(api.GET("/api/v1/deployments", { params: { query: { status: f.status, project_id: f.project_id } } }))
      }
      rowKey={(d) => d.id}
      columns={[
        { header: "Version", cell: (d) => d.version },
        { header: "Status", cell: (d) => <Badge>{d.status}</Badge> },
        {
          header: "Health",
          cell: (d) => <Badge tone={d.health === "healthy" ? "success" : d.health ? "danger" : "neutral"}>{d.health ?? "—"}</Badge>,
        },
        { header: "Target", cell: (d) => d.target },
        { header: "Commit", cell: (d) => <span className="font-mono text-xs">{d.commit_sha.slice(0, 10)}</span> },
        { header: "Project", cell: (d) => <ProjectLink id={d.project_id} /> },
        { header: "Approver", cell: (d) => d.approver_id ?? "—" },
        { header: "Deployed", cell: (d) => formatTime(d.deployed_at) },
      ]}
      emptyText="Nothing has been deployed yet."
    />
  );
}
