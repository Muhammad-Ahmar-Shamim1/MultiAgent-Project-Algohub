"use client";

import { useQuery } from "@tanstack/react-query";
import { useParams } from "next/navigation";
import * as React from "react";

import { PageHeader, ProjectLink, QueryState } from "@/components/common";
import { QaReport, SecurityReport } from "@/components/reports";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardTitle } from "@/components/ui/card";
import { ErrorText, Hint, Input, Label, Textarea } from "@/components/ui/form";
import { api, artifactUrl, unwrap } from "@/lib/api/client";
import { formatTime, humanize } from "@/lib/format";

type Json = Record<string, unknown>;

function record(value: unknown): Json | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Json) : null;
}

function DecisionForm({ approvalId, onDecided }: { approvalId: string; onDecided: () => void }) {
  const [comment, setComment] = React.useState("");
  const [approver, setApprover] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  async function decide(decision: "approve" | "reject") {
    setError(null);
    if (!approver.trim()) {
      setError("Enter your approver name or ID.");
      return;
    }
    if (decision === "reject" && !comment.trim()) {
      setError("Say what must change - the comment becomes the fix task.");
      return;
    }
    setBusy(true);
    try {
      await unwrap(
        api.POST("/api/v1/approvals/{approval_id}", {
          params: { path: { approval_id: approvalId } },
          body: { decision, comment: comment.trim() || null, approver_id: approver.trim() },
        }),
      );
      onDecided();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not record the decision");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={(e) => e.preventDefault()} noValidate className="space-y-3">
      <div>
        <Label htmlFor="approver">Approver</Label>
        <Input id="approver" value={approver} onChange={(e) => setApprover(e.target.value)} maxLength={128} />
      </div>
      <div>
        <Label htmlFor="comment">Comment</Label>
        <Textarea
          id="comment"
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          aria-describedby="comment-hint"
        />
        <Hint id="comment-hint">Required to reject: it is attached to the fix task.</Hint>
      </div>
      {error ? <ErrorText>{error}</ErrorText> : null}
      <div className="flex flex-wrap gap-2">
        <Button type="button" variant="success" disabled={busy} onClick={() => decide("approve")}>
          Approve
        </Button>
        <Button type="button" variant="danger" disabled={busy} onClick={() => decide("reject")}>
          Reject
        </Button>
      </div>
    </form>
  );
}

export default function ApprovalPage() {
  const { id } = useParams<{ id: string }>();
  const approval = useQuery({
    queryKey: ["approval", id],
    queryFn: () => unwrap(api.GET("/api/v1/approvals/{approval_id}", { params: { path: { approval_id: id } } })),
  });
  const whoami = useQuery({
    queryKey: ["whoami"],
    queryFn: () => unwrap(api.GET("/api/v1/whoami")),
    refetchInterval: false,
  });

  const data = approval.data;
  const evidence = record(data?.evidence) ?? {};
  const diff = record(evidence.diff);
  const plan = record(evidence.deployment_plan);
  const screenshots = Array.isArray(evidence.screenshots) ? (evidence.screenshots as string[]) : [];

  return (
    <QueryState isLoading={approval.isLoading} error={approval.error}>
      {data ? (
        <>
          <PageHeader
            title={`Approve: ${humanize(data.action_type)}`}
            description={
              <span>
                {data.summary} · project <ProjectLink id={data.project_id} /> · requested{" "}
                {formatTime(data.created_at)}
              </span>
            }
          />

          <Card className="mb-4" aria-labelledby="decision-heading">
            <h2 id="decision-heading" className="mb-3 text-base font-semibold">
              Decision
            </h2>
            {data.decision ? (
              <p className="text-sm">
                <Badge tone={data.decision.decision === "approve" ? "success" : "danger"}>
                  {data.decision.decision === "approve" ? "Approved" : "Rejected"}
                </Badge>{" "}
                by {data.decision.approver_id}
                {data.decision.comment ? `: “${data.decision.comment}”` : ""}
              </p>
            ) : whoami.data?.role === "approver" ? (
              <DecisionForm approvalId={id} onDecided={() => void approval.refetch()} />
            ) : (
              <p className="text-sm text-muted-foreground">
                Awaiting an approver. You are signed in as {whoami.data?.role ?? "…"}.
              </p>
            )}
          </Card>

          {/* IR-004: diff, test report, security report and screenshots on one page. */}
          <div className="grid gap-4 lg:grid-cols-2">
            <Card>
              <CardTitle>Changes</CardTitle>
              {diff ? (
                <>
                  <p className="mb-2 font-mono text-xs break-all">
                    {String(diff.base ?? "").slice(0, 10)} → {String(diff.head ?? "").slice(0, 10)}
                  </p>
                  <pre className="max-h-80 overflow-auto rounded-md bg-muted p-3 text-xs" aria-label="Diff summary">
                    {String(diff.stat ?? "")}
                  </pre>
                  {Array.isArray(diff.commits) && diff.commits.length ? (
                    <details className="mt-2 text-sm" open>
                      <summary className="cursor-pointer">Commits ({diff.commits.length})</summary>
                      <ul className="mt-1 font-mono text-xs">
                        {(diff.commits as string[]).map((c) => (
                          <li key={c} className="break-all">
                            {c}
                          </li>
                        ))}
                      </ul>
                    </details>
                  ) : null}
                </>
              ) : (
                <p className="text-sm text-muted-foreground">
                  {typeof evidence.note === "string" ? evidence.note : "No diff available."}
                </p>
              )}
            </Card>

            <Card>
              <CardTitle>Deployment plan</CardTitle>
              {plan ? (
                <div className="text-sm">
                  <p>
                    Target <strong>{String(plan.target)}</strong>
                    {plan.commit ? ` · commit ${String(plan.commit).slice(0, 10)}` : ""}{" "}
                    <Badge>{String(plan.status ?? "")}</Badge>
                  </p>
                  <ol className="mt-2 list-decimal pl-5">
                    {(Array.isArray(plan.steps) ? (plan.steps as string[]) : []).map((step) => (
                      <li key={step}>{step}</li>
                    ))}
                  </ol>
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">No plan attached.</p>
              )}
            </Card>

            <Card>
              <CardTitle>Test report</CardTitle>
              <QaReport report={evidence.qa_report} />
            </Card>

            <Card>
              <CardTitle>Security report</CardTitle>
              <SecurityReport report={evidence.security_report} />
            </Card>
          </div>

          <Card className="mt-4">
            <CardTitle>Screenshots</CardTitle>
            {screenshots.length ? (
              <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {screenshots.map((path) => (
                  <li key={path}>
                    <figure>
                      {/* Served by the platform through the proxy; not a Next static asset. */}
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img
                        src={artifactUrl(data.project_id, path)}
                        alt={`Screenshot ${path}`}
                        className="w-full rounded-md border"
                        loading="lazy"
                      />
                      <figcaption className="mt-1 text-xs break-all text-muted-foreground">{path}</figcaption>
                    </figure>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted-foreground">No screenshots were captured.</p>
            )}
          </Card>
        </>
      ) : null}
    </QueryState>
  );
}
