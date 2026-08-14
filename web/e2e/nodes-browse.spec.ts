import { expect, test, type Page } from "@playwright/test";

const PASSWORD = process.env.NODUM_E2E_PASSWORD ?? "e2e-secret-password";

async function signIn(page: Page): Promise<void> {
  await page.goto("/");
  const username = page.locator('input[name="username"]');
  await username.waitFor({ state: "visible" });
  await username.fill("owner");
  await page.locator('input[name="current-password"]').fill(PASSWORD);
  await page.locator('button[type="submit"]').click();
  await expect(username).toBeHidden();
}

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test("browses a bounded node set with URL-backed filters and sorting", async ({ page }) => {
  await page.getByRole("link", { name: "Nodes" }).click();
  await expect(page).toHaveURL(/\/nodes$/);
  await expect(page.getByRole("heading", { name: "Nodes" })).toBeVisible();
  await expect(page.getByText(/up to 500 oldest matching nodes/i)).toBeVisible();
  await expect(page.getByRole("link", { name: "Alpha node" })).toBeVisible();

  await page.getByLabel("Node type").selectOption("concept");
  await expect(page).toHaveURL(/type=concept/);
  await page.getByLabel("State").selectOption("active");
  await expect(page).toHaveURL(/state=active/);
  await page.getByLabel("Sort").selectOption("title-desc");
  await expect(page).toHaveURL(/sort=title-desc/);

  const titles = page.locator(".nd-nodes-row__title");
  await expect(titles).toHaveText(["Beta node", "Alpha node"]);

  await page.reload();
  await expect(page.getByLabel("Node type")).toHaveValue("concept");
  await expect(page.getByLabel("State")).toHaveValue("active");
  await expect(page.getByLabel("Sort")).toHaveValue("title-desc");

  await page.getByLabel("State").selectOption("");
  await expect(page).not.toHaveURL(/state=/);
  await page.goBack();
  await expect(page.getByLabel("State")).toHaveValue("active");
  await page.goForward();
  await expect(page.getByLabel("State")).toHaveValue("");
});

test("space cards open the filtered browse view and controls remain keyboard accessible", async ({
  page,
}) => {
  await page.goto("/spaces");
  const browseLink = page.getByRole("link", { name: "Browse nodes" }).first();
  await browseLink.focus();
  await expect(browseLink).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/nodes\?space=/);

  await page.setViewportSize({ width: 390, height: 844 });
  const typeFilter = page.locator('select[name="node-type"]');
  await expect(typeFilter).toBeEnabled();
  await typeFilter.focus();
  await expect(typeFilter).toBeFocused();
  await expect(page.getByRole("heading", { name: "Nodes" })).toBeVisible();
});

test("deep-linked retired filters stay visible and fail with actionable copy", async ({ page }) => {
  await page.goto("/nodes?space=Retired%20research");
  await expect(page.locator('select[name="node-space"]')).toContainText(
    "Retired research (archived)",
  );
  await expect(page.getByText("That space filter could not be applied")).toBeVisible();
  await expect(page.getByText(/has been archived/)).toBeVisible();
  await expect(page.getByText("Could not load nodes")).toBeVisible();
  await expect(page.getByText(/unknown space/i)).toHaveCount(0);
  await page.getByRole("button", { name: "Clear space filter" }).click();
  await expect(page).toHaveURL(/\/nodes$/);
  await expect(page.getByRole("link", { name: "Alpha node" })).toBeVisible();

  await page.goto("/nodes?type=retired-type");
  await expect(page.locator('select[name="node-type"]')).toContainText(
    "retired-type (unavailable)",
  );
  await expect(page.locator('select[name="node-type"]')).toHaveValue("retired-type");
  await expect(page.getByText("That node type filter could not be applied")).toBeVisible();
  await expect(page.getByText(/shared URL has been preserved/)).toBeVisible();
  await page.getByRole("button", { name: "Clear type filter" }).click();
  await expect(page).toHaveURL(/\/nodes$/);
});
