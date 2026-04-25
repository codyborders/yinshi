import { expect, test } from "@playwright/test";

import { authenticateContext, uniqueEmail } from "./helpers/testApp";

test("settings shows runner storage options", async ({ page }) => {
  await authenticateContext(page.context(), uniqueEmail("runner-options"));
  await page.goto("/app/settings");

  await page.getByRole("tab", { name: "Cloud runner" }).click();

  await expect(page.getByRole("heading", { name: "Cloud Runner" })).toBeVisible();
  await expect(page.getByRole("radio", { name: /Hosted Yinshi/ })).toBeChecked();
  await expect(page.getByRole("radio", { name: /AWS BYOC: EBS plus S3 Files/ })).toBeVisible();
  await expect(page.getByRole("radio", { name: /Archil shared-files mode/ })).toBeVisible();
  await expect(page.getByRole("radio", { name: /Archil all-POSIX mode/ })).toBeVisible();
  await expect(page.getByText("Archil-managed active storage/cache").first()).toBeVisible();
});


test("settings can add and remove a BYOK key without exposing the raw secret", async ({ page }) => {
  const rawKey = "sk-openai-playwright-secret-key";

  await authenticateContext(page.context(), uniqueEmail("settings"));
  await page.goto("/app/settings");

  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();

  const openAiCard = page
    .locator("div.rounded-xl", {
      has: page.getByRole("heading", { name: "OpenAI" }),
    })
    .first();
  await openAiCard.getByPlaceholder("Label (optional)").fill("Work key");
  await openAiCard.getByPlaceholder("Enter API key").fill(rawKey);
  await openAiCard.getByRole("button", { name: "Save Connection" }).click();

  await expect(openAiCard).toContainText("Connected");
  await expect(openAiCard).toContainText("Work key");
  await expect(page.locator("body")).not.toContainText(rawKey);

  await openAiCard.getByRole("button", { name: "Remove" }).click();
  await expect(openAiCard).toContainText("Not connected");
});
