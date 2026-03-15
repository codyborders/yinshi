import { expect, test } from "@playwright/test";

import { authenticateContext } from "./helpers/testApp";

test("landing renders and the auth entrypoint redirects", async ({ page, request }) => {
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "Yinshi" })).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Sign In / Sign Up" }),
  ).toHaveAttribute("href", "/auth/login");

  const response = await request.get("/auth/login", { maxRedirects: 0 });
  expect(response.status()).toBe(307);
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
