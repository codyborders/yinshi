import { expect, test } from "@playwright/test";

import {
  authenticateContext,
  createLocalRepo,
  seedFullStack,
  storeApiKey,
  uniqueEmail,
} from "./helpers/testApp";

test("slash commands render the expected session responses", async ({ page }) => {
  const email = uniqueEmail("slash");
  const authSession = await authenticateContext(page.context(), email);
  await storeApiKey(authSession, "anthropic", "sk-ant-playwright", "Anthropic");
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
  await expect(
    page.getByText("Current model: MiniMax M2.7 (minimax/MiniMax-M2.7)"),
  ).toBeVisible();

  await composer.fill("/model sonnet");
  await composer.press("Enter");
  await expect(
    page.getByText(
      "Model changed to Claude Sonnet 4 (anthropic/claude-sonnet-4-20250514)",
    ),
  ).toBeVisible();

  await composer.fill("/tree");
  await composer.press("Enter");
  await expect(page.getByText("Workspace files")).toBeVisible();
  await expect(page.getByText("README.md")).toBeVisible();

  await composer.fill("/clear");
  await composer.press("Enter");
  await expect(page.getByText("Send a message to start coding.")).toBeVisible();
});
