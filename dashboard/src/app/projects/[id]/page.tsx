"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "next/navigation";
import * as React from "react";

import { PageHeader, QueryState, StateBadge } from "@/components/common";
import { EventFeed } from "@/components/event-feed";
import { MermaidGraph } from "@/components/mermaid-graph";
import { QaReport, SecurityReport } from "@/components/reports";
import { Card, CardTitle } from "@/components/ui/card";
import { Table, Td, Th } from "@/components/ui/table";
import { ApiError, api, artifactUrl, unwrap } from "@/lib/api/client";
import { formatTime, humanize } from "@/lib/format";

/** A query that treats 404 ("nothing yet") as an empty result instead of an error. */
async function orNull<T>(call: Promise<T>): Promise<T | null> {
  try {
    return await call;
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

export default function ProjectPage() {
  const { id } = useParams<{ id: string }>();
  const path = { params: { path: { project_id: id } } };
  const queryClient = useQueryClient();

  const project = useQuery({
    queryKey: ["project", id],
    queryFn: () => unwrap(api.GET("/api/v1/projects/{project_id}", path)),
  });
  const timeline = useQuery({
    queryKey: ["timeline", id],
    queryFn: () => unwrap(api.GET("/api/v1/projects/{project_id}/timeline", path)),
  });
  const plan = useQuery({
    queryKey: ["plan", id],
    queryFn: () => unwrap(api.GET("/api/v1/projects/{project_id}/plan", path)),
  });
  const warRoom = useQuery({
    queryKey: ["war-room", id],
    queryFn: () => orNull(unwrap(api.GET("/api/v1/projects/{project_id}/war-room", path))),
  });
  const reports = useQuery({
    queryKey: ["reports", id],
    queryFn: () => unwrap(api.GET("/api/v1/projects/{project_id}/reports", path)),
  });

  // A pushed event means something changed: refresh the views that depend on it.
  const refresh = React.useCallback(() => {
    for (const key of ["project", "timeline", "plan", "war-room", "reports"]) {
      void queryClient.invalidateQueries({ queryKey: [key, id] });
    }
  }, [queryClient, id]);

  const hasTasks = (plan.data?.mermaid ?? "").split("\n").length > 1;

  return (
    <>
      <QueryState isLoading={project.isLoading} error={project.error}>
        {project.data ? (
          <PageHeader
            title={project.data.title}
            description={
              <span className="flex flex-wrap items-center gap-2">
                <StateBadge state={project.data.state} /> requested by {project.data.requester_id}
              </span>
            }
          />
        ) : null}
      </QueryState>

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="space-y-4 lg:col-span-2">
          <Card>
            <CardTitle>Task graph</CardTitle>
            <QueryState
              isLoading={plan.isLoading}
              error={plan.error}
              isEmpty={!hasTasks}
              emptyText="No plan yet - tasks appear once planning is done."
            >
              {plan.data ? <MermaidGraph source={plan.data.mermaid} label="Task dependency graph" /> : null}
            </QueryState>
          </Card>

          <Card>
            <CardTitle>QA report</CardTitle>
            <QueryState isLoading={reports.isLoading} error={reports.error}>
              <QaReport report={reports.data?.qa} />
            </QueryState>
          </Card>

          <Card>
            <CardTitle>Security report</CardTitle>
            <QueryState isLoading={reports.isLoading} error={reports.error}>
              <SecurityReport report={reports.data?.security} />
            </QueryState>
          </Card>

          <Card>
            <CardTitle>War room</CardTitle>
            <QueryState
              isLoading={warRoom.isLoading}
              error={warRoom.error}
              isEmpty={!warRoom.data}
              emptyText="The team has not been formed yet."
            >
              {warRoom.data ? (
                <div className="space-y-4">
                  <Table aria-label="Team members">
                    <thead>
                      <tr>
                        <Th>Agent</Th>
                        <Th>Role</Th>
                        <Th>State</Th>
                      </tr>
                    </thead>
                    <tbody>
                      {warRoom.data.members.map((m) => (
                        <tr key={m.agent_id}>
                          <Td className="font-mono text-xs">{m.display_id}</Td>
                          <Td>{humanize(m.role)}</Td>
                          <Td>
                            <StateBadge state={m.state} />
                          </Td>
                        </tr>
                      ))}
                    </tbody>
                  </Table>
                  <div>
                    <h3 className="mb-1 text-sm font-semibold">Artifacts</h3>
                    {warRoom.data.artifacts.length ? (
                      <ul className="space-y-1 text-sm">
                        {warRoom.data.artifacts.map((a) => (
                          <li key={a.path}>
                            <a
                              className="break-all text-primary underline-offset-2 hover:underline"
                              href={artifactUrl(id, a.path)}
                              target="_blank"
                              rel="noreferrer"
                            >
                              {a.path}
                            </a>{" "}
                            <span className="text-xs text-muted-foreground">({humanize(a.kind)})</span>
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <p className="text-sm text-muted-foreground">No artifacts yet.</p>
                    )}
                  </div>
                </div>
              ) : null}
            </QueryState>
          </Card>
        </div>

        <div className="space-y-4">
          <Card>
            <CardTitle>State timeline</CardTitle>
            <QueryState
              isLoading={timeline.isLoading}
              error={timeline.error}
              isEmpty={!timeline.data?.length}
              emptyText="Just received."
            >
              <ol className="space-y-2 border-l pl-4">
                {timeline.data?.map((entry, index) => (
                  <li key={`${entry.state}-${index}`} className="text-sm">
                    <StateBadge state={entry.state} />
                    <time className="ml-2 text-xs text-muted-foreground" dateTime={entry.timestamp}>
                      {formatTime(entry.timestamp)}
                    </time>
                  </li>
                ))}
              </ol>
            </QueryState>
          </Card>

          <Card>
            <CardTitle>Live events</CardTitle>
            <EventFeed projectId={id} onEvent={refresh} />
          </Card>
        </div>
      </div>
    </>
  );
}
