import { expect, test } from "@playwright/test";

test("home page renders the golden-v1 landing content", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "golden-v1" })).toBeVisible();
  await expect(page.getByText("backend:")).toBeVisible();
});
