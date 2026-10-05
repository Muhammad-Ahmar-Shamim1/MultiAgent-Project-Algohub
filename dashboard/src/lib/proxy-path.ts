/**
 * Validation for the dashboard's /api/platform/* proxy: only the platform's own versioned API
 * may be reached through it, never another path on that host.
 */

const SEGMENT = /^[A-Za-z0-9._~:@!$&'()*+,;=-]+$/;

/** The upstream path for proxy segments, or null if they must not be forwarded. */
export function upstreamPath(segments: readonly string[]): string | null {
  if (segments.length < 3 || segments[0] !== "api" || segments[1] !== "v1") {
    return null;
  }
  for (const segment of segments) {
    if (segment === "" || segment === "." || segment === ".." || !SEGMENT.test(segment)) {
      return null;
    }
  }
  return "/" + segments.map(encodeURIComponent).join("/");
}

/** Request headers worth forwarding upstream (everything else, cookies included, is dropped). */
export const FORWARDED_REQUEST_HEADERS = ["accept", "content-type", "last-event-id"] as const;

/** Response headers worth passing back to the browser. */
export const FORWARDED_RESPONSE_HEADERS = [
  "content-type",
  "cache-control",
  "content-disposition",
] as const;
