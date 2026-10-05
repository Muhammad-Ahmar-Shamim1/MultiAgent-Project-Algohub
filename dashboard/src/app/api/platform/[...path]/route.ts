import { cookies } from "next/headers";
import type { NextRequest } from "next/server";

import {
  FORWARDED_REQUEST_HEADERS,
  FORWARDED_RESPONSE_HEADERS,
  upstreamPath,
} from "@/lib/proxy-path";
import { SESSION_COOKIE, platformApiUrl } from "@/lib/session";

// The browser talks only to this route; it adds the user's bearer token (from the httpOnly
// session cookie) and forwards to the platform API. Responses - including the SSE event
// stream - are streamed straight back. The token never reaches client-side JavaScript.
export const dynamic = "force-dynamic";

async function forward(request: NextRequest, ctx: RouteContext<"/api/platform/[...path]">) {
  const { path } = await ctx.params;
  const target = upstreamPath(path);
  if (target === null) {
    return Response.json({ detail: "Not a platform API path" }, { status: 404 });
  }
  const token = (await cookies()).get(SESSION_COOKIE)?.value;
  if (!token) {
    return Response.json({ detail: "Not signed in" }, { status: 401 });
  }

  const headers = new Headers({ Authorization: `Bearer ${token}` });
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  const hasBody = request.method !== "GET" && request.method !== "HEAD";

  let upstream: Response;
  try {
    upstream = await fetch(`${platformApiUrl()}${target}${request.nextUrl.search}`, {
      method: request.method,
      headers,
      body: hasBody ? await request.text() : undefined,
      cache: "no-store",
      signal: request.signal,
    });
  } catch {
    return Response.json({ detail: "The platform API is unreachable" }, { status: 502 });
  }

  const responseHeaders = new Headers();
  for (const name of FORWARDED_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }
  if (responseHeaders.get("content-type")?.startsWith("text/event-stream")) {
    responseHeaders.set("X-Accel-Buffering", "no");
  }
  return new Response(upstream.body, { status: upstream.status, headers: responseHeaders });
}

export const GET = forward;
export const POST = forward;
export const PUT = forward;
