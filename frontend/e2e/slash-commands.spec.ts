import { expect, test } from "@playwright/test";

import {
  authenticateContext,
  createLocalRepo,
  seedFullStack,
  uniqueEmail,
} from "./helpers/testApp";

test("slash commands render the expected session responses", async ({ page }) => {
  const email = uniqueEmail("slash");
  const authSession = await authenticateContext(page.context(), email);
  const repoPath = createLocalRepo("slash");
  const seeded = await seedFullStack(authSession, repoPath);
  const composer = page.getByPlaceholder("Describe what to build...");

  await page.goto(`/app/session/${seeded.session.id}`);

  await composer.fill("/help");
  await composer.press("Enter");
  await expect(page.getByText("Available commands:")).toBeVisible();

  await composer.fill("/model");
  await composer.press("Enter");
  await expect(page.getByText("Current model:")).toBeVisible();
  await expect(page.getByText("minimax")).toBeVisible();

  await composer.fill("/model sonnet");
  await composer.press("Enter");
  await expect(page.getByText("Model changed to sonnet")).toBeVisible();

  await composer.fill("/tree");
  await composer.press("Enter");
  await expect(page.getByText("Workspace files")).toBeVisible();
  await expect(page.getByText("README.md")).toBeVisible();

  await composer.fill("/clear");
  await composer.press("Enter");
  await expect(page.getByText("Send a message to start coding.")).toBeVisible();
});
