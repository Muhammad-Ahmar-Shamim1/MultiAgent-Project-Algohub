/** Small presentation helpers shared by every page. */

export type Tone = "neutral" | "info" | "success" | "warning" | "danger";

const DANGER = new Set(["BLOCKED", "FAILED", "FIX_REQUIRED", "REJECTED", "CANCELLED"]);
const WARNING = new Set(["AWAITING_APPROVAL", "REVIEW", "DEPENDENCY_WAITING", "ANALYZING"]);
const SUCCESS = new Set(["COMPLETED", "AVAILABLE", "MONITORING", "APPROVED", "RETIRED"]);
const INFO = new Set([
  "IN_PROGRESS",
  "WORKING",
  "ASSIGNED",
  "DEVELOPING",
  "DEPLOYING",
  "TESTING",
  "INTEGRATING",
  "SECURITY_REVIEW",
  "PLANNING",
  "ARCHITECTING",
]);

/** Which colour a state badge gets. */
export function stateTone(state: string): Tone {
  if (DANGER.has(state)) return "danger";
  if (WARNING.has(state)) return "warning";
  if (SUCCESS.has(state)) return "success";
  if (INFO.has(state)) return "info";
  return "neutral";
}

/** "IN_PROGRESS" -> "In progress". */
export function humanize(value: string): string {
  const words = value.replace(/[_-]+/g, " ").trim().toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** A timestamp as local date + time, or an em dash. */
export function formatTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}

const ATTENTION_ORDER = ["approval_pending", "project_failed", "project_blocked", "task_blocked", "task_failed"];

/** The never-hidden list (FR-163), most urgent kinds first, stable within a kind. */
export function sortAttention<T extends { kind: string }>(items: readonly T[]): T[] {
  const rank = (kind: string) => {
    const index = ATTENTION_ORDER.indexOf(kind);
    return index === -1 ? ATTENTION_ORDER.length : index;
  };
  return [...items].sort((a, b) => rank(a.kind) - rank(b.kind));
}

/** Label for an attention item's kind. */
export function attentionLabel(kind: string): string {
  const labels: Record<string, string> = {
    approval_pending: "Awaiting approval",
    project_failed: "Project failed",
    project_blocked: "Project blocked",
    task_blocked: "Task blocked",
    task_failed: "Tests failed",
  };
  return labels[kind] ?? humanize(kind);
}

/** Split a textarea into its non-empty lines; split a comma list into its non-empty items. */
export function lines(text: string): string[] {
  return text.split(/\r?\n/).map((l) => l.replace(/^[-*]\s*/, "").trim()).filter(Boolean);
}

export function commaList(text: string): string[] {
  return text.split(",").map((t) => t.trim()).filter(Boolean);
}

/** The intake API has one free-text description; roles and features become labelled sections of
 * it so the requirement analyzer sees them as the requester structured them (IR-002). */
export function composeDescription(description: string, roles: string[], features: string[]): string {
  const parts = [description.trim()];
  if (roles.length) parts.push(`User roles: ${roles.join(", ")}`);
  if (features.length) parts.push(`Features:\n${features.map((f) => `- ${f}`).join("\n")}`);
  return parts.join("\n\n");
}
