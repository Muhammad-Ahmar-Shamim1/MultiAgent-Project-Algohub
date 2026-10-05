"use client";

import * as React from "react";

/** Renders the planner's Mermaid flowchart (FR-162). Mermaid is loaded only on pages using it,
 * runs with ``securityLevel: "strict"`` (no HTML/script in labels), and follows the theme. */
export function MermaidGraph({ source, label }: { source: string; label: string }) {
  const ref = React.useRef<HTMLDivElement>(null);
  const [error, setError] = React.useState<string | null>(null);
  const id = React.useId().replace(/[^a-zA-Z0-9]/g, "");

  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        const dark = document.documentElement.classList.contains("dark");
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          theme: dark ? "dark" : "default",
        });
        const { svg } = await mermaid.render(`plan-${id}`, source);
        if (!cancelled && ref.current) {
          ref.current.innerHTML = svg;
          setError(null);
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "could not render the plan");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [source, id]);

  if (error) {
    return (
      <div>
        <p role="alert" className="mb-2 text-sm text-danger">
          The task graph could not be drawn ({error}). Its source:
        </p>
        <pre className="overflow-x-auto rounded-md bg-muted p-3 text-xs">{source}</pre>
      </div>
    );
  }
  return (
    <div
      ref={ref}
      role="img"
      aria-label={label}
      className="mermaid-graph overflow-x-auto rounded-md bg-background p-2"
    />
  );
}
