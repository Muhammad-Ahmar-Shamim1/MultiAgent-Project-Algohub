import { defineConfig, devices } from "@playwright/test";

const PORT = 3100;

// The platform API is never contacted: every test intercepts the browser's calls to the
// dashboard's own /api/platform/* proxy (tests/e2e/fixtures.ts), so these run without the
// Python stack. PLATFORM_API_URL points at a closed port to make any leak fail loudly.
export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: true,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "on-first-retry",
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
    { name: "phone-360", use: { ...devices["Desktop Chrome"], viewport: { width: 360, height: 740 } } },
  ],
  webServer: {
    command: `npm run dev -- --port ${PORT} --hostname 127.0.0.1`,
    url: `http://127.0.0.1:${PORT}/login`,
    reuseExistingServer: !process.env.CI,
    timeout: 180_000,
    env: { PLATFORM_API_URL: "http://127.0.0.1:9" },
  },
});
