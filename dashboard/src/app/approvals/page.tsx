"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import * as React from "react";

import { PageHeader, ProjectLink, QueryState } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Label, Select } from "@/components/ui/form";
import { Table, Td, Th } from "@/components/ui/table";
import { api, unwrap } from "@/lib/api/client";
import { formatTime, humanize } from "@/lib/format";

export default function ApprovalsPage() {
  const [show, setShow] = React.useState("pending");
  const query = useQuery({
    queryKey: ["approvals", show],
    queryFn: () =>
      unwrap(api.GET("/api/v1/approvals", { params: { query: { pending: show === "pending" } } })),
  });

  return (
    <>
      <PageHeader title="Approvals" description="Human sign-off for deployments and other sensitive actions." />
      <div className="mb-4 max-w-xs">
        <Label htmlFor="show">Show</Label>
        <Select id="show" value={show} onChange={(e) => setShow(e.target.value)}>
          <option value="pending">Awaiting a decision</option>
          <option value="all">All requests</option>
        </Select>
      </div>
      <QueryState
        isLoading={query.isLoading}
        error={query.error}
        isEmpty={!query.data?.length}
        emptyText="No approval requests."
      >
        <Table aria-label="Approval requests">
          <thead>
            <tr>
              <Th>Request</Th>
              <Th>Project</Th>
              <Th>Decision</Th>
              <Th>Requested</Th>
            </tr>
          </thead>
          <tbody>
            {query.data?.map((a) => (
              <tr key={a.id}>
                <Td>
                  <Link href={`/approvals/${a.id}`} className="font-medium text-primary hover:underline">
                    {humanize(a.action_type)}
                  </Link>
                  <p className="text-xs text-muted-foreground">{a.summary}</p>
                </Td>
                <Td>
                  <ProjectLink id={a.project_id} />
                </Td>
                <Td>
                  {a.decision ? (
                    <Badge tone={a.decision.decision === "approve" ? "success" : "danger"}>
                      {a.decision.decision === "approve" ? "Approved" : "Rejected"}
                    </Badge>
                  ) : (
                    <Badge tone="warning">Awaiting</Badge>
                  )}
                </Td>
                <Td>{formatTime(a.created_at)}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </QueryState>
    </>
  );
}
