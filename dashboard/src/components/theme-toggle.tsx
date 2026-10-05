"use client";

import * as React from "react";

import { Button } from "@/components/ui/button";

type Theme = "light" | "dark";

function currentTheme(): Theme {
  return document.documentElement.classList.contains("dark") ? "dark" : "light";
}

/** Switches light/dark and remembers the choice; the first paint uses ``themeScript``. */
export function ThemeToggle() {
  const [theme, setTheme] = React.useState<Theme | null>(null);
  React.useEffect(() => setTheme(currentTheme()), []);

  function toggle() {
    const next: Theme = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.classList.toggle("dark", next === "dark");
    try {
      localStorage.setItem("theme", next);
    } catch {
      // storage can be unavailable (private mode); the toggle still works for this page
    }
    setTheme(next);
  }

  return (
    <Button
      variant="outline"
      size="sm"
      onClick={toggle}
      aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
    >
      {theme === "dark" ? "Light" : "Dark"}
    </Button>
  );
}

/** Runs before first paint (inline in <head>) so the page never flashes the wrong theme. */
export const themeScript = [
  "(function(){try{",
  "var t=localStorage.getItem('theme');",
  "var d=t?t==='dark':window.matchMedia('(prefers-color-scheme: dark)').matches;",
  "document.documentElement.classList.toggle('dark',d);",
  "}catch(e){}})();",
].join("");
