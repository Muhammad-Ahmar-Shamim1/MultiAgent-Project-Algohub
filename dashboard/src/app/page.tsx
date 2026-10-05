"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";

import { PageHeader, QueryState, StateBadge } from "@/components/common";
import { buttonVariants } from "@/components/ui/button";
import { Card, CardTitle } from "@/components/ui/card";
import { api, unwrap } from "@/lib/api/client";
import { attentionLabel, sortAttention } from "@/lib/format";
import { cn } from "@/lib/utils";

const FINISHED = new Set(["COMPLETED", "FAILED"]);

function attentionHref(item: { kind: string; id: string; project_id?: string | null }): string {
  if (item.kind === "approval_pending") return `/approvals/${item.id}`;
  if (item.kind.startsWith("project_")) return `/projects/${item.id}`;
  return item.project_id ? `/projects/${item.project_id}` : "/tasks";
}

function Tile({ label, value, tone }: { label: string; value: number; tone?: "danger" | "warning" }) {
  return (
    <Card
      className={cn(
        "p-3",
        tone === "danger" && value > 0 && "border-danger/50 bg-danger-bg",
        tone === "warning" && value > 0 && "border-warning/50 bg-warning-bg",
      )}
    >
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-1 text-2xl font-semibold" data-testid={`tile-${label}`}>
        {value}
      </p>
    </Card>
  );
}

export default function OverviewPage() {
  const overview = useQuery({
    queryKey: ["overview"],
    queryFn: () => unwrap(api.GET("/api/v1/overview")),
  });
  const projects = useQuery({
    queryKey: ["projects", "recent"],
    queryFn: () => unwrap(api.GET("/api/v1/projects", { params: { query: { limit: 20 } } })),
  });
  const data = overview.data;
  const attention = data ? sortAttention(data.attention) : [];
  const active = (projects.data ?? []).filter((p) => !FINISHED.has(p.state));

  return (
    <>
      <PageHeader
        title="Overview"
        actions={
          <Link href="/projects/new" className={buttonVariants()}>
            New project
          </Link>
        }
      />
      <QueryState isLoading={overview.isLoading} error={overview.error}>
        {/* FR-163: blocked, failed and awaiting-approval items are never hidden or collapsed. */}
        <section
          aria-labelledby="attention-heading"
          className={cn(
            "mb-6 rounded-lg border p-4",
            attention.length ? "border-warning/60 bg-warning-bg" : "bg-card",
          )}
        >
          <h2 id="attention-heading" className="mb-2 font-semibold">
            Needs attention ({attention.length})
          </h2>
          {attention.length ? (
            <ul className="space-y-1">
              {attention.map((item) => (
                <li key={`${item.kind}-${item.id}`} className="flex flex-wrap items-center gap-2 text-sm">
                  <span className="rounded bg-background px-2 py-0.5 text-xs font-semibold text-warning">
                    {attentionLabel(item.kind)}
                  </span>
                  <Link className="break-all underline underline-offset-2" href={attentionHref(item)}>
                    {item.label}
                  </Link>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted-foreground">Nothing is blocked, failing or awaiting approval.</p>
          )}
        </section>

        {data ? (
          <section aria-label="Totals" className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
            <Tile label="Agents" value={data.agents_total} />
            <Tile label="Agents available" value={data.agents_available} />
            <Tile label="Agents working" value={data.agents_working} />
            <Tile label="Agents blocked" value={data.agents_blocked} tone="danger" />
            <Tile label="Active projects" value={data.projects_active} />
            <Tile label="Blocked projects" value={data.projects_blocked} tone="danger" />
            <Tile label="Pending tasks" value={data.tasks_pending} />
            <Tile label="Tasks in progress" value={data.tasks_in_progress} />
            <Tile label="Failed tasks" value={data.tasks_failed} tone="danger" />
            <Tile label="Blocked tasks" value={data.tasks_blocked} tone="danger" />
            <Tile label="Deployments" value={data.deployments} />
            <Tile label="Pending approvals" value={data.approvals_pending} tone="warning" />
          </section>
        ) : null}
      </QueryState>

      <Card>
        <CardTitle>Active projects</CardTitle>
        <QueryState
          isLoading={projects.isLoading}
          error={projects.error}
          isEmpty={!active.length}
          emptyText="No active projects."
        >
          <ul className="divide-y">
            {active.map((p) => (
              <li key={p.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                <Link href={`/projects/${p.id}`} className="font-medium underline-offset-2 hover:underline">
                  {p.title}
                </Link>
                <StateBadge state={p.state} />
              </li>
            ))}
          </ul>
        </QueryState>
      </Card>
    </>
  );
}
