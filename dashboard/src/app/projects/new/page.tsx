"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import * as React from "react";

import { PageHeader, StateBadge } from "@/components/common";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardTitle } from "@/components/ui/card";
import { ErrorText, Hint, Input, Label, Textarea } from "@/components/ui/form";
import { ApiError, api, unwrap } from "@/lib/api/client";
import { commaList, composeDescription, lines } from "@/lib/format";

const STILL_ANALYSING = new Set(["RECEIVED", "ANALYZING"]);

/** Step 2 (IR-002): watch the analysis and answer its clarification questions inline. */
function Clarifications({ projectId }: { projectId: string }) {
  const project = useQuery({
    queryKey: ["project", projectId],
    queryFn: () =>
      unwrap(api.GET("/api/v1/projects/{project_id}", { params: { path: { project_id: projectId } } })),
    refetchInterval: 3_000,
  });
  const requirements = useQuery({
    queryKey: ["requirements", projectId],
    queryFn: async () => {
      try {
        return await unwrap(
          api.GET("/api/v1/projects/{project_id}/requirements", {
            params: { path: { project_id: projectId } },
          }),
        );
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) return null; // not analysed yet
        throw err;
      }
    },
    refetchInterval: 3_000,
  });
  const [answers, setAnswers] = React.useState<Record<string, string>>({});
  const [error, setError] = React.useState<string | null>(null);
  const [sent, setSent] = React.useState(false);

  const state = project.data?.state;
  const questions = requirements.data?.open_questions ?? [];
  const analysing = !state || STILL_ANALYSING.has(state);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    const filled = Object.fromEntries(Object.entries(answers).filter(([, a]) => a.trim()));
    try {
      await unwrap(
        api.POST("/api/v1/projects/{project_id}/clarifications", {
          params: { path: { project_id: projectId } },
          body: { answers: filled },
        }),
      );
      setSent(true);
      await Promise.all([project.refetch(), requirements.refetch()]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not send the answers");
    }
  }

  return (
    <Card aria-live="polite">
      <CardTitle>Request received</CardTitle>
      <p className="mb-3 flex flex-wrap items-center gap-2 text-sm">
        Status: {state ? <StateBadge state={state} /> : "…"}
      </p>
      {analysing && questions.length && !sent ? (
        <form onSubmit={submit} noValidate>
          <p className="mb-3 text-sm">
            The analyst needs a few answers before planning. Unanswered questions fall back to
            sensible defaults.
          </p>
          {questions.map((question, index) => (
            <div key={question} className="mb-3">
              <Label htmlFor={`q-${index}`}>{question}</Label>
              <Textarea
                id={`q-${index}`}
                className="min-h-16"
                value={answers[question] ?? ""}
                onChange={(e) => setAnswers((a) => ({ ...a, [question]: e.target.value }))}
              />
            </div>
          ))}
          {error ? <ErrorText>{error}</ErrorText> : null}
          <Button type="submit" disabled={!Object.values(answers).some((a) => a.trim())}>
            Send answers
          </Button>
        </form>
      ) : analysing ? (
        <p role="status" className="text-sm text-muted-foreground">
          {sent ? "Answers sent - re-analysing…" : "Analysing your request…"}
        </p>
      ) : (
        <p className="text-sm">Analysis is complete and the project is under way.</p>
      )}
      <Link href={`/projects/${projectId}`} className={buttonVariants({ variant: "outline", className: "mt-4" })}>
        Open project
      </Link>
    </Card>
  );
}

export default function NewProjectPage() {
  const [form, setForm] = React.useState({
    title: "",
    description: "",
    roles: "",
    features: "",
    constraints: "",
    requester: "",
  });
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  const [submitError, setSubmitError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [projectId, setProjectId] = React.useState<string | null>(null);

  const set = (key: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
    setForm((f) => ({ ...f, [key]: e.target.value }));

  function validate(): Record<string, string> {
    const found: Record<string, string> = {};
    if (!form.title.trim()) found.title = "Give the project a title.";
    if (!form.description.trim()) found.description = "Describe what you want built.";
    if (!lines(form.features).length) found.features = "List at least one feature.";
    if (!form.requester.trim()) found.requester = "Enter your name or ID.";
    return found;
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const found = validate();
    setErrors(found);
    if (Object.keys(found).length) return;
    setBusy(true);
    setSubmitError(null);
    try {
      const project = await unwrap(
        api.POST("/api/v1/projects", {
          body: {
            title: form.title.trim(),
            description: composeDescription(form.description, commaList(form.roles), lines(form.features)),
            constraints: form.constraints.trim() || null,
            requester_id: form.requester.trim(),
          },
        }),
      );
      setProjectId(project.id);
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : "Could not submit the request");
    } finally {
      setBusy(false);
    }
  }

  if (projectId) {
    return (
      <>
        <PageHeader title={form.title} description="Your request is being analysed." />
        <Clarifications projectId={projectId} />
      </>
    );
  }

  const field = (key: string) => ({
    "aria-invalid": errors[key] ? true : undefined,
    "aria-describedby": errors[key] ? `${key}-error` : `${key}-hint`,
  });

  return (
    <>
      <PageHeader title="New project" description="Describe the web app you need; the department plans and builds it." />
      <Card className="max-w-3xl">
        <form onSubmit={submit} noValidate className="space-y-4">
          <div>
            <Label htmlFor="title">Title</Label>
            <Input id="title" value={form.title} onChange={set("title")} maxLength={300} {...field("title")} />
            {errors.title ? <ErrorText id="title-error">{errors.title}</ErrorText> : <Hint id="title-hint">e.g. Clothing e-commerce shop</Hint>}
          </div>
          <div>
            <Label htmlFor="description">Description</Label>
            <Textarea id="description" value={form.description} onChange={set("description")} {...field("description")} />
            {errors.description ? (
              <ErrorText id="description-error">{errors.description}</ErrorText>
            ) : (
              <Hint id="description-hint">Who it is for and what problem it solves.</Hint>
            )}
          </div>
          <div>
            <Label htmlFor="roles">User roles</Label>
            <Input id="roles" value={form.roles} onChange={set("roles")} {...field("roles")} />
            <Hint id="roles-hint">Comma-separated, e.g. customer, admin.</Hint>
          </div>
          <div>
            <Label htmlFor="features">Features</Label>
            <Textarea id="features" value={form.features} onChange={set("features")} {...field("features")} />
            {errors.features ? (
              <ErrorText id="features-error">{errors.features}</ErrorText>
            ) : (
              <Hint id="features-hint">One per line, e.g. &quot;Cart with quantity changes&quot;.</Hint>
            )}
          </div>
          <div>
            <Label htmlFor="constraints">Constraints (optional)</Label>
            <Textarea id="constraints" value={form.constraints} onChange={set("constraints")} {...field("constraints")} />
            <Hint id="constraints-hint">Deadlines, hosting, compliance, anything fixed.</Hint>
          </div>
          <div>
            <Label htmlFor="requester">Your name or ID</Label>
            <Input id="requester" value={form.requester} onChange={set("requester")} maxLength={128} {...field("requester")} />
            {errors.requester ? <ErrorText id="requester-error">{errors.requester}</ErrorText> : <Hint id="requester-hint">Recorded as the requester.</Hint>}
          </div>
          {submitError ? <ErrorText>{submitError}</ErrorText> : null}
          <Button type="submit" disabled={busy}>
            {busy ? "Submitting…" : "Submit request"}
          </Button>
        </form>
      </Card>
    </>
  );
}
