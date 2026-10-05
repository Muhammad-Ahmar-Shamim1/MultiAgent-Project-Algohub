/** The httpOnly cookie that carries the user's platform bearer token. Never readable from JS. */
export const SESSION_COOKIE = "algohub_session";

/** Sessions last a working day; the user signs in again after that. */
export const SESSION_MAX_AGE_SECONDS = 12 * 60 * 60;

/** Where the server-side proxy reaches the platform API (server env only - never shipped). */
export function platformApiUrl(): string {
  return (process.env.PLATFORM_API_URL ?? "http://localhost:8000").replace(/\/+$/, "");
}
