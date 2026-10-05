import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { buildApiUrl } from "@/lib/api-client";

describe("buildApiUrl", () => {
  const originalUrl = process.env.NEXT_PUBLIC_API_URL;

  beforeEach(() => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.com/";
  });

  afterEach(() => {
    process.env.NEXT_PUBLIC_API_URL = originalUrl;
  });

  it("joins the configured base URL with a leading-slash path", () => {
    expect(buildApiUrl("/health")).toBe("https://api.example.com/health");
  });

  it("tolerates a path with no leading slash", () => {
    expect(buildApiUrl("health")).toBe("https://api.example.com/health");
  });

  it("falls back to localhost:8000 when the env var is unset", () => {
    delete process.env.NEXT_PUBLIC_API_URL;
    expect(buildApiUrl("/health")).toBe("http://localhost:8000/health");
  });
});
