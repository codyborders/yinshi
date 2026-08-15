/* Covers public landing navigation and the authenticated mobile drawer through Playwright. */
import { expect, test } from "@playwright/test";

import { authenticateContext } from "./helpers/testApp";

test("landing renders and targets GitHub login", async ({ page }) => {
  await page.goto("/");

  await expect(
    page.getByRole("heading", {
      name: "Run coding agents against your repositories from any browser.",
    }),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: "Sign in" })).toHaveAttribute(
    "href",
    "/auth/login/github",
  );
});

test("mobile navigation toggles the authenticated sidebar", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await authenticateContext(page.context());

  await page.goto("/app");

  const overlay = page.getByTestId("sidebar-overlay");
  await expect(overlay).toHaveCount(0);

  await page.getByLabel("Toggle sidebar").click();
  await expect(overlay).toBeVisible();

  await overlay.click();
  await expect(overlay).toHaveCount(0);
});

test("mobile Settings title clears the sidebar toggle", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await authenticateContext(page.context());

  await page.goto("/app/settings");

  const toggleBox = await page.getByLabel("Toggle sidebar").boundingBox();
  const titleBox = await page
    .getByRole("heading", { name: "Settings", level: 1 })
    .boundingBox();

  expect(toggleBox).not.toBeNull();
  expect(titleBox).not.toBeNull();
  expect(titleBox!.y).toBeGreaterThanOrEqual(toggleBox!.y + toggleBox!.height);
});
