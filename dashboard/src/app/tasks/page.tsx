"use client";

import { ProjectLink, StateBadge } from "@/components/common";
import { ListPage } from "@/components/list-page";
import { api, unwrap, type Schemas } from "@/lib/api/client";
import { formatTime, humanize } from "@/lib/format";
import { ROLES, TASK_STATES } from "@/lib/states";

export default function TasksPage() {
  return (
    <ListPage<Schemas["TaskListItemOut"]>
      title="Tasks"
      description="Every task across every project."
      queryKey="tasks"
      filters={[
        { key: "state", label: "State", kind: "select", options: TASK_STATES },
        { key: "role", label: "Role", kind: "select", options: ROLES },
        { key: "project_id", label: "Project ID", kind: "text", placeholder: "prj_…" },
      ]}
      fetch={(f) =>
        unwrap(
          api.GET("/api/v1/tasks", {
            params: {
              query: {
                state: f.state as Schemas["TaskState"] | undefined,
                role: f.role,
                project_id: f.project_id,
              },
            },
          }),
        )
      }
      rowKey={(t) => t.id}
      columns={[
        { header: "Task", cell: (t) => <span className="font-mono text-xs">{t.display_id}</span> },
        { header: "Title", cell: (t) => <span className="block max-w-md whitespace-normal">{t.title}</span> },
        { header: "Role", cell: (t) => humanize(t.role) },
        { header: "State", cell: (t) => <StateBadge state={t.state} /> },
        { header: "Retries", cell: (t) => t.retries },
        { header: "Project", cell: (t) => <ProjectLink id={t.project_id} /> },
        { header: "Updated", cell: (t) => formatTime(t.updated_at) },
      ]}
      emptyText="No tasks match."
    />
  );
}
