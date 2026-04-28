import path from "node:path";

import { expect, test } from "@playwright/test";

import {
  authenticateContext,
  createLocalRepo,
  seedFullStack,
  storeApiKey,
  uniqueEmail,
} from "./helpers/testApp";

test("golden path onboarding imports a repo and persists chat history", async ({ page }) => {
  const email = uniqueEmail("onboarding");
  const repoPath = createLocalRepo("onboarding");
  const repoName = path.basename(repoPath);
  const prompt = "Fix login form validation";
  const assistantReply = `Mock reply for: ${prompt}`;

  const authSession = await authenticateContext(page.context(), email);
  await storeApiKey(
    authSession,
    "minimax",
    "sk-minimax-playwright-onboarding",
    "Playwright MiniMax",
  );
  await page.goto("/app");

  await expect(page.getByText("No repositories yet.")).toBeVisible();
  await page.getByRole("button", { name: "Add repository" }).last().click();
  await page
    .getByPlaceholder("GitHub URL, user/repo, or local path")
    .fill(repoPath);
  await page.getByRole("button", { name: "Import" }).click();

  await expect(page.getByText(repoName)).toBeVisible();
  await page.getByText(repoName).hover();
  await page.getByTitle("New branch").click();

  await expect(page).toHaveURL(/\/app\/session\//);

  await page.getByPlaceholder("Describe what to build...").fill(prompt);
  await page.getByLabel("Send").click();

  await expect(page.getByText(assistantReply, { exact: true })).toBeVisible();
  await expect(page.getByText(prompt, { exact: true })).toBeVisible();

  await page.reload();

  await expect(page.getByText("fix-login-form")).toBeVisible();
  await expect(page.getByText(prompt, { exact: true })).toBeVisible();
  await expect(page.getByText(assistantReply, { exact: true })).toBeVisible();
});

test("seeded chat sessions stream responses and survive reloads", async ({ page }) => {
  const email = uniqueEmail("session");
  const authSession = await authenticateContext(page.context(), email);
  await storeApiKey(
    authSession,
    "minimax",
    "sk-minimax-playwright-session",
    "Playwright MiniMax",
  );
  const repoPath = createLocalRepo("seeded");
  const seeded = await seedFullStack(authSession, repoPath);
  const prompt = "Summarize the repository";
  const assistantReply = `Mock reply for: ${prompt}`;

  await page.goto(`/app/session/${seeded.session.id}`);

  await page.getByPlaceholder("Describe what to build...").fill(prompt);
  await page.getByLabel("Send").click();

  await expect(page.getByText("Streaming")).toBeVisible();
  await expect(page.getByText(assistantReply, { exact: true })).toBeVisible();

  await page.reload();

  await expect(page.getByText(prompt, { exact: true })).toBeVisible();
  await expect(page.getByText(assistantReply, { exact: true })).toBeVisible();
});
