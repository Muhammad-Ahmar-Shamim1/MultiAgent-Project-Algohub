import createClient from "openapi-fetch";

import type { components, paths } from "./schema";

/** Typed platform API client. Calls go to the dashboard's own server-side proxy, never directly
 * to the platform, so no token is ever handled in the browser. */
export const api = createClient<paths>({ baseUrl: "/api/platform" });

export type Schemas = components["schemas"];

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** The ``detail`` of a FastAPI error body, as readable text. */
export function errorDetail(error: unknown): string {
  if (error && typeof error === "object" && "detail" in error) {
    const detail = (error as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((d) => (d && typeof d === "object" && "msg" in d ? String(d.msg) : String(d)))
        .join("; ");
    }
  }
  return "Something went wrong";
}

/** Await an openapi-fetch call and return its data, or throw ``ApiError`` (401 -> sign in). */
export async function unwrap<T>(
  call: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  const { data, error, response } = await call;
  if (response.status === 401 && typeof window !== "undefined") {
    window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
  }
  if (error !== undefined || data === undefined) {
    throw new ApiError(response.status, errorDetail(error));
  }
  return data;
}

/** URL of one project artifact (screenshot, report), served through the proxy. */
export function artifactUrl(projectId: string, path: string): string {
  const encoded = path.split("/").map(encodeURIComponent).join("/");
  return `/api/platform/api/v1/projects/${encodeURIComponent(projectId)}/artifacts/${encoded}`;
}

/** URL of a project's live event stream (SSE), served through the proxy. */
export function eventStreamUrl(projectId: string): string {
  return `/api/platform/api/v1/projects/${encodeURIComponent(projectId)}/events?from_id=0`;
}
