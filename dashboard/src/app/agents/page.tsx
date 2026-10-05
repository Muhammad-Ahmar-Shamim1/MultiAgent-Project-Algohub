"use client";

import { ProjectLink, StateBadge } from "@/components/common";
import { ListPage } from "@/components/list-page";
import { api, unwrap, type Schemas } from "@/lib/api/client";
import { formatTime, humanize } from "@/lib/format";
import { AGENT_STATES, ROLES } from "@/lib/states";

export default function AgentsPage() {
  return (
    <ListPage<Schemas["AgentOut"]>
      title="Agents"
      description="Workers created for each project's plan."
      queryKey="agents"
      filters={[
        { key: "state", label: "State", kind: "select", options: AGENT_STATES },
        { key: "role", label: "Role", kind: "select", options: ROLES },
        { key: "project_id", label: "Project ID", kind: "text", placeholder: "prj_…" },
      ]}
      fetch={(f) =>
        unwrap(
          api.GET("/api/v1/agents", {
            params: {
              query: {
                state: f.state as Schemas["AgentState"] | undefined,
                role: f.role,
                project_id: f.project_id,
              },
            },
          }),
        )
      }
      rowKey={(a) => a.id}
      columns={[
        { header: "Agent", cell: (a) => <span className="font-mono text-xs">{a.display_id}</span> },
        { header: "Role", cell: (a) => (a.role ? humanize(a.role) : "—") },
        { header: "State", cell: (a) => <StateBadge state={a.state} /> },
        { header: "Project", cell: (a) => <ProjectLink id={a.project_id} /> },
        { header: "Blueprint", cell: (a) => `${a.blueprint_id}@${a.blueprint_version}` },
        { header: "Updated", cell: (a) => formatTime(a.updated_at) },
      ]}
      emptyText="No agents match."
    />
  );
}
