"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import * as React from "react";

import { ThemeToggle } from "@/components/theme-toggle";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const LINKS = [
  { href: "/", label: "Overview" },
  { href: "/projects", label: "Projects" },
  { href: "/approvals", label: "Approvals" },
  { href: "/agents", label: "Agents" },
  { href: "/tasks", label: "Tasks" },
  { href: "/workstations", label: "Workstations" },
  { href: "/knowledge", label: "Knowledge" },
  { href: "/deployments", label: "Deployments" },
  { href: "/monitoring", label: "Monitoring" },
];

function isCurrent(pathname: string, href: string): boolean {
  return href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);
}

export function Nav() {
  const pathname = usePathname();
  const [open, setOpen] = React.useState(false);

  if (pathname === "/login") {
    return null;
  }

  async function signOut() {
    await fetch("/api/session", { method: "DELETE" });
    window.location.assign("/login");
  }

  return (
    <header className="border-b bg-card">
      <div className="mx-auto flex max-w-7xl items-center justify-between gap-2 px-4 py-3">
        <Link href="/" className="text-lg font-semibold">
          AlgoHub <span className="text-muted-foreground">Web Dept.</span>
        </Link>
        <div className="flex items-center gap-2">
          <ThemeToggle />
          <Button variant="outline" size="sm" onClick={signOut}>
            Sign out
          </Button>
          <Button
            variant="outline"
            size="sm"
            className="lg:hidden"
            aria-expanded={open}
            aria-controls="main-nav"
            onClick={() => setOpen((v) => !v)}
          >
            Menu
          </Button>
        </div>
      </div>
      <nav
        id="main-nav"
        aria-label="Main"
        className={cn("mx-auto max-w-7xl px-4 pb-3 lg:block", open ? "block" : "hidden")}
      >
        <ul className="flex flex-col gap-1 lg:flex-row lg:flex-wrap">
          {LINKS.map((link) => {
            const current = isCurrent(pathname, link.href);
            return (
              <li key={link.href}>
                <Link
                  href={link.href}
                  onClick={() => setOpen(false)}
                  aria-current={current ? "page" : undefined}
                  className={cn(
                    "block rounded-md px-3 py-2 text-sm hover:bg-muted",
                    current && "bg-muted font-semibold",
                  )}
                >
                  {link.label}
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>
    </header>
  );
}
