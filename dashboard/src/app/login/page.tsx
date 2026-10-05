"use client";

import { useRouter, useSearchParams } from "next/navigation";
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ErrorText, Hint, Input, Label } from "@/components/ui/form";

function safeNext(value: string | null): string {
  // Only same-site paths - never an open redirect to another origin.
  return value && value.startsWith("/") && !value.startsWith("//") ? value : "/";
}

function LoginForm() {
  const router = useRouter();
  const next = safeNext(useSearchParams().get("next"));
  const [token, setToken] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const response = await fetch("/api/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
    setBusy(false);
    if (!response.ok) {
      const body = (await response.json().catch(() => ({}))) as { detail?: string };
      setError(body.detail ?? "Sign-in failed");
      return;
    }
    router.replace(next);
    router.refresh();
  }

  return (
    <Card className="mx-auto mt-12 max-w-md">
      <h1 className="mb-1 text-xl font-semibold">Sign in to AlgoHub</h1>
      <p className="mb-4 text-sm text-muted-foreground">
        Use the requester, approver or operator token your administrator gave you.
      </p>
      <form onSubmit={submit} noValidate>
        <Label htmlFor="token">Access token</Label>
        <Input
          id="token"
          type="password"
          autoComplete="current-password"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          aria-invalid={error ? true : undefined}
          aria-describedby="token-hint"
          required
        />
        <Hint id="token-hint">
          It is kept in a secure, HTTP-only cookie on this server - never in the browser&apos;s
          scripts.
        </Hint>
        {error ? <ErrorText>{error}</ErrorText> : null}
        <Button type="submit" className="mt-4 w-full" disabled={busy || !token}>
          {busy ? "Signing in…" : "Sign in"}
        </Button>
      </form>
    </Card>
  );
}

export default function LoginPage() {
  return (
    <React.Suspense>
      <LoginForm />
    </React.Suspense>
  );
}
