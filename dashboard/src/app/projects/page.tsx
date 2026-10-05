"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import * as React from "react";

import { PageHeader, QueryState, StateBadge } from "@/components/common";
import { buttonVariants } from "@/components/ui/button";
import { Label, Select } from "@/components/ui/form";
import { Table, Td, Th } from "@/components/ui/table";
import { api, unwrap, type Schemas } from "@/lib/api/client";
import { formatTime, humanize } from "@/lib/format";

const STATES = [
  "RECEIVED", "ANALYZING", "PLANNING", "ARCHITECTING", "DEVELOPING", "INTEGRATING", "TESTING",
  "SECURITY_REVIEW", "AWAITING_APPROVAL", "DEPLOYING", "MONITORING", "COMPLETED", "FAILED",
  "BLOCKED",
] as const;

export default function ProjectsPage() {
  const [state, setState] = React.useState("");
  const query = useQuery({
    queryKey: ["projects", state],
    queryFn: () =>
      unwrap(
        api.GET("/api/v1/projects", {
          params: { query: { state: (state || undefined) as Schemas["ProjectState"] | undefined } },
        }),
      ),
  });

  return (
    <>
      <PageHeader
        title="Projects"
        actions={
          <Link href="/projects/new" className={buttonVariants()}>
            New project
          </Link>
        }
      />
      <div className="mb-4 max-w-xs">
        <Label htmlFor="state-filter">State</Label>
        <Select id="state-filter" value={state} onChange={(e) => setState(e.target.value)}>
          <option value="">All states</option>
          {STATES.map((s) => (
            <option key={s} value={s}>
              {humanize(s)}
            </option>
          ))}
        </Select>
      </div>
      <QueryState
        isLoading={query.isLoading}
        error={query.error}
        isEmpty={!query.data?.length}
        emptyText="No projects match."
      >
        <Table aria-label="Projects">
          <thead>
            <tr>
              <Th>Title</Th>
              <Th>State</Th>
              <Th>Requester</Th>
              <Th>Updated</Th>
            </tr>
          </thead>
          <tbody>
            {query.data?.map((p) => (
              <tr key={p.id}>
                <Td>
                  <Link href={`/projects/${p.id}`} className="font-medium text-primary hover:underline">
                    {p.title}
                  </Link>
                </Td>
                <Td>
                  <StateBadge state={p.state} />
                </Td>
                <Td>{p.requester_id}</Td>
                <Td>{formatTime(p.updated_at)}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </QueryState>
    </>
  );
}
