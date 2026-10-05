import Link from "next/link";
import * as React from "react";

import { Badge } from "@/components/ui/badge";
import { humanize, stateTone } from "@/lib/format";

export function StateBadge({ state }: { state: string }) {
  return <Badge tone={stateTone(state)}>{humanize(state)}</Badge>;
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: React.ReactNode;
  actions?: React.ReactNode;
}) {
  return (
    <div className="mb-6 flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        <h1 className="text-2xl font-semibold break-words">{title}</h1>
        {description ? <p className="mt-1 text-sm text-muted-foreground">{description}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap gap-2">{actions}</div> : null}
    </div>
  );
}

/** Loading / error / empty states for a TanStack query, so every page handles all three. */
export function QueryState({
  isLoading,
  error,
  isEmpty,
  emptyText = "Nothing here yet.",
  children,
}: {
  isLoading: boolean;
  error: unknown;
  isEmpty?: boolean;
  emptyText?: string;
  children: React.ReactNode;
}) {
  if (isLoading) {
    return (
      <p role="status" className="text-sm text-muted-foreground">
        Loading…
      </p>
    );
  }
  if (error) {
    return (
      <p
        role="alert"
        className="rounded-md border border-danger/40 bg-danger-bg p-3 text-sm text-danger"
      >
        Could not load: {error instanceof Error ? error.message : "unknown error"}
      </p>
    );
  }
  if (isEmpty) {
    return <p className="text-sm text-muted-foreground">{emptyText}</p>;
  }
  return <>{children}</>;
}

export function ProjectLink({
  id,
  children,
}: {
  id: string | null | undefined;
  children?: React.ReactNode;
}) {
  if (!id) return <span>—</span>;
  return (
    <Link
      className="font-mono text-xs text-primary underline-offset-2 hover:underline"
      href={`/projects/${id}`}
    >
      {children ?? id}
    </Link>
  );
}
