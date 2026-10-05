import { cookies } from "next/headers";
import type { NextRequest } from "next/server";

import { SESSION_COOKIE, SESSION_MAX_AGE_SECONDS, platformApiUrl } from "@/lib/session";

export const dynamic = "force-dynamic";

/** Sign in: check the token against the platform, then keep it in an httpOnly cookie. */
export async function POST(request: NextRequest) {
  let token = "";
  try {
    const body = (await request.json()) as { token?: unknown };
    token = typeof body.token === "string" ? body.token.trim() : "";
  } catch {
    // fall through to the empty-token error
  }
  if (!token) {
    return Response.json({ detail: "Enter your access token" }, { status: 400 });
  }

  let role: string;
  try {
    const response = await fetch(`${platformApiUrl()}/api/v1/whoami`, {
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
    });
    if (response.status === 401) {
      return Response.json({ detail: "That token was not accepted" }, { status: 401 });
    }
    if (!response.ok) {
      return Response.json({ detail: "The platform API refused the sign-in" }, { status: 502 });
    }
    role = ((await response.json()) as { role: string }).role;
  } catch {
    return Response.json({ detail: "The platform API is unreachable" }, { status: 502 });
  }

  (await cookies()).set(SESSION_COOKIE, token, {
    httpOnly: true,
    sameSite: "strict",
    secure: request.nextUrl.protocol === "https:",
    path: "/",
    maxAge: SESSION_MAX_AGE_SECONDS,
  });
  return Response.json({ role });
}

/** Sign out. */
export async function DELETE() {
  (await cookies()).delete(SESSION_COOKIE);
  return new Response(null, { status: 204 });
}
