/**
 * Thin fetch wrapper for the backend API. Every request from the frontend goes through
 * here (TEMPLATE_GUIDE.md: "never a raw fetch() scattered in a component"), and the base
 * URL always comes from `NEXT_PUBLIC_API_URL` (TEMPLATE_GUIDE.md rule 1: settings only
 * from environment).
 */

const DEFAULT_API_URL = "http://localhost:8000";

/** The backend's base URL, read from the one environment variable the browser can see. */
export function getApiUrl(): string {
  return process.env.NEXT_PUBLIC_API_URL ?? DEFAULT_API_URL;
}

/** Join the API base URL with a path, tolerating either side's leading/trailing slash. */
export function buildApiUrl(path: string): string {
  const base = getApiUrl().replace(/\/+$/, "");
  const suffix = path.startsWith("/") ? path : `/${path}`;
  return `${base}${suffix}`;
}

export interface HealthResponse {
  status: string;
}

/** Call the backend's `GET /health` endpoint. */
export async function getHealth(): Promise<HealthResponse> {
  const response = await fetch(buildApiUrl("/health"), { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`GET /health failed: ${response.status}`);
  }
  return (await response.json()) as HealthResponse;
}
