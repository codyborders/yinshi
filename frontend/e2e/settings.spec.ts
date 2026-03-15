import { expect, test } from "@playwright/test";

import { authenticateContext, uniqueEmail } from "./helpers/testApp";

test("settings can add and remove a BYOK key without exposing the raw secret", async ({ page }) => {
  const rawKey = "sk-ant-playwright-secret-key";

  await authenticateContext(page.context(), uniqueEmail("settings"));
  await page.goto("/app/settings");

  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();

  await page.getByRole("combobox").selectOption("anthropic");
  await page.getByPlaceholder("Label (optional)").fill("Work key");
  await page.getByPlaceholder("sk-...").fill(rawKey);
  await page.getByRole("button", { name: "Add Key" }).click();

  const keyItem = page.locator("li").filter({ hasText: "Work key" });
  await expect(keyItem).toContainText("anthropic");
  await expect(keyItem).toContainText("Work key");
  await expect(page.locator("body")).not.toContainText(rawKey);

  await page.getByRole("button", { name: "Remove" }).click();
  await expect(page.getByText("No API keys configured.")).toBeVisible();
});
