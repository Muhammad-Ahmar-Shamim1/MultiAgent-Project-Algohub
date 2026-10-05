"use client";

import { useEffect, useState } from "react";

import { getHealth } from "@/lib/api-client";

type Status = "checking" | "up" | "down";

/** Shows whether the backend is reachable. One component, one job. */
export function HealthStatus() {
  const [status, setStatus] = useState<Status>("checking");

  useEffect(() => {
    let cancelled = false;
    getHealth()
      .then(() => {
        if (!cancelled) setStatus("up");
      })
      .catch(() => {
        if (!cancelled) setStatus("down");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const color =
    status === "up" ? "bg-green-500" : status === "down" ? "bg-red-500" : "bg-zinc-400";

  return (
    <p className="flex items-center gap-2 text-sm text-zinc-600 dark:text-zinc-400">
      <span className={`h-2 w-2 rounded-full ${color}`} aria-hidden />
      backend: {status}
    </p>
  );
}
