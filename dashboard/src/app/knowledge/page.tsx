"use client";

import { ProjectLink } from "@/components/common";
import { ListPage } from "@/components/list-page";
import { api, unwrap, type Schemas } from "@/lib/api/client";
import { formatTime, humanize } from "@/lib/format";

export default function KnowledgePage() {
  return (
    <ListPage<Schemas["KnowledgeOut"]>
      title="Knowledge"
      description="What the department has learned, by layer."
      queryKey="knowledge"
      filters={[
        { key: "layer", label: "Layer", kind: "select", options: ["global", "web", "project", "memory"] },
        { key: "project_id", label: "Project ID", kind: "text", placeholder: "prj_…" },
      ]}
      fetch={(f) =>
        unwrap(api.GET("/api/v1/knowledge", { params: { query: { layer: f.layer, project_id: f.project_id } } }))
      }
      rowKey={(k) => k.id}
      columns={[
        { header: "Layer", cell: (k) => humanize(k.layer) },
        { header: "Excerpt", cell: (k) => <span className="block max-w-lg whitespace-normal">{k.excerpt}</span> },
        { header: "Source", cell: (k) => <span className="break-all">{k.source}</span> },
        { header: "Version", cell: (k) => k.version },
        { header: "Project", cell: (k) => <ProjectLink id={k.project_id} /> },
        { header: "Updated", cell: (k) => formatTime(k.updated_at) },
      ]}
      emptyText="No knowledge entries yet."
    />
  );
}
