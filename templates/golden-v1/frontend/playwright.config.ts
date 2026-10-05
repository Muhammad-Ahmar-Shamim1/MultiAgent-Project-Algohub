import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: true,
  // junit: AlgoHub's QA runner (web_department/qa/test_runner.py) reads this file to build its
  // report - keep the path in sync with its JUNIT_DIR.
  reporter: [["list"], ["junit", { outputFile: "../artifacts/junit/e2e.xml" }]],
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL ?? "http://localhost:3000",
    trace: "on-first-retry",
  },
});
