/* Covers public landing navigation and the authenticated mobile drawer through Playwright. */
import { expect, test } from "@playwright/test";

import {
  authenticateContext,
  createLocalRepo,
  seedFullStack,
  uniqueEmail,
} from "./helpers/testApp";

test("landing renders and targets configured login", async ({ page }) => {
  await page.goto("/");

  await expect(
    page.getByRole("heading", {
      name: "Run coding agents against your repositories from any browser.",
    }),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: "Sign in" })).toHaveAttribute(
    "href",
    "/auth/login",
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

test("mobile workspace controls render and switch panels", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });

  const email = uniqueEmail("workspace-controls");
  const authSession = await authenticateContext(page.context(), email);
  const repoPath = createLocalRepo("workspace-controls");
  const seeded = await seedFullStack(authSession, repoPath);

  await page.goto(`/app/session/${seeded.session.id}`);

  // Small bounding-box helper: assert a locator is visible and fully inside viewport.
  async function assertInViewport(
    locator: ReturnType<typeof page.locator>,
    name: string,
  ) {
    await expect(locator).toBeVisible();
    const box = await locator.boundingBox();
    expect(box).not.toBeNull();
    const x = box!.x;
    const y = box!.y;
    const w = box!.width;
    const h = box!.height;
    expect(x).toBeGreaterThanOrEqual(0);
    expect(y).toBeGreaterThanOrEqual(0);
    expect(x + w).toBeLessThanOrEqual(390);
    expect(y + h).toBeLessThanOrEqual(844);
  }

  // All header controls visible and in-viewport before any interaction.
  const filesButton = page.getByRole("button", { name: "Files" });
  const terminalButton = page.getByRole("button", { name: "Terminal" });
  const modelSelect = page.locator("#session-model");
  const thinkingSelect = page.locator("#thinking-level");

  // Screenshot: initial session state (before any clicks).
  await page.screenshot({
    path: test.info().outputPath("00-initial-session.png"),
  });

  await assertInViewport(filesButton, "Files");
  await assertInViewport(terminalButton, "Terminal");
  await assertInViewport(modelSelect, "Model");
  await assertInViewport(thinkingSelect, "Thinking");

  // Click Files — explorer mode opens.
  await filesButton.click();
  await expect(
    page.getByRole("button", { name: "Close workspace" }),
  ).toBeVisible();
  await expect(page.getByText("All files")).toBeVisible();
  await expect(
    page.getByRole("complementary", { name: "Workspace files" }),
  ).toBeVisible();

  // Screenshot: Files mode.
  await page.screenshot({
    path: test.info().outputPath("01-files-mode.png"),
  });

  // Click Terminal — overlay switches to terminal mode.
  await page
    .getByRole("button", { name: "Close workspace" })
    .locator("..")
    .getByRole("button", { name: "Terminal" })
    .click();
  await expect(
    page.getByRole("button", { name: "Close workspace" }),
  ).toBeVisible();
  await expect(
    page.getByRole("complementary", { name: "Workspace terminal" }),
  ).toBeVisible();
  await expect(page.getByText("All files")).not.toBeVisible();

  // Screenshot: Terminal mode.
  await page.screenshot({
    path: test.info().outputPath("02-terminal-mode.png"),
  });

  // Click Close — overlay disappears, chat visible again.
  await page.getByRole("button", { name: "Close workspace" }).click();
  await expect(
    page.getByRole("button", { name: "Close workspace" }),
  ).not.toBeVisible();
  await expect(
    page.getByPlaceholder("Describe what to build..."),
  ).toBeVisible();
});
