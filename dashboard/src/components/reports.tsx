import * as React from "react";

import { Badge } from "@/components/ui/badge";
import { Table, Td, Th } from "@/components/ui/table";

type Json = Record<string, unknown>;

function asRecord(value: unknown): Json {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Json) : {};
}

function asList(value: unknown): Json[] {
  return Array.isArray(value) ? value.map(asRecord) : [];
}

/** The QA report (qa/test_runner.py's QAReport): counts, criteria coverage, failures. */
export function QaReport({ report }: { report: unknown }) {
  if (!report) {
    return <p className="text-sm text-muted-foreground">QA has not produced a report yet.</p>;
  }
  const r = asRecord(report);
  const coverage = Object.entries(asRecord(r.criteria_coverage));
  const failures = asList(r.failures);
  return (
    <div className="space-y-3">
      <p className="flex flex-wrap gap-2 text-sm">
        <Badge tone="success">{String(r.passed ?? 0)} passed</Badge>
        <Badge tone={Number(r.failed) ? "danger" : "neutral"}>{String(r.failed ?? 0)} failed</Badge>
        <Badge>{String(r.skipped ?? 0)} skipped</Badge>
      </p>
      {coverage.length ? (
        <Table aria-label="Acceptance criteria coverage">
          <thead>
            <tr>
              <Th>Criterion</Th>
              <Th>Status</Th>
            </tr>
          </thead>
          <tbody>
            {coverage.map(([id, status]) => (
              <tr key={id}>
                <Td className="font-mono text-xs">{id}</Td>
                <Td>
                  <Badge tone={status === "pass" ? "success" : status === "fail" ? "danger" : "warning"}>
                    {String(status)}
                  </Badge>
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      ) : null}
      {failures.length ? (
        <ul className="space-y-2">
          {failures.map((f, i) => (
            <li key={i} className="rounded-md border border-danger/40 bg-danger-bg p-2 text-sm">
              <p className="font-mono text-xs break-all">{String(f.test)}</p>
              <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap text-xs">
                {String(f.message ?? "")}
              </pre>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

const SECURITY_SECTIONS = [
  ["blocking", "Blocking (high/critical)", "danger"],
  ["warnings", "Warnings (medium)", "warning"],
  ["excepted", "Accepted exceptions", "neutral"],
] as const;

/** The security report (security/security_gate.py's SecurityReport). */
export function SecurityReport({ report }: { report: unknown }) {
  if (!report) {
    return <p className="text-sm text-muted-foreground">No security scan has run yet.</p>;
  }
  const r = asRecord(report);
  return (
    <div className="space-y-3">
      <p className="text-sm">
        <Badge tone={r.passed ? "success" : "danger"}>{r.passed ? "Gate passed" : "Gate blocked"}</Badge>
      </p>
      {asList(r.scanners).length ? (
        <p className="flex flex-wrap gap-2 text-xs">
          {asList(r.scanners).map((s) => (
            <Badge key={String(s.tool)} tone={s.status === "ok" ? "success" : s.status === "error" ? "danger" : "warning"}>
              {String(s.tool)}: {String(s.status)}
            </Badge>
          ))}
        </p>
      ) : null}
      {SECURITY_SECTIONS.map(([key, title, tone]) => {
        const findings = asList(r[key]);
        if (!findings.length) return null;
        return (
          <div key={key}>
            <h3 className="mb-1 text-sm font-semibold">{title}</h3>
            <ul className="space-y-1">
              {findings.map((f, i) => (
                <li key={i} className="rounded-md border p-2 text-sm">
                  <Badge tone={tone}>{String(f.severity)}</Badge>{" "}
                  <span className="font-mono text-xs">
                    {String(f.tool)} · {String(f.rule)} · {String(f.file ?? "-")}:{String(f.line ?? "-")}
                  </span>
                  <p className="mt-1 break-words">{String(f.message ?? "")}</p>
                </li>
              ))}
            </ul>
          </div>
        );
      })}
    </div>
  );
}
