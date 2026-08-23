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

/** The text input of one settings row. */
function rowInput(page: Page, key: string) {
  return page.locator(`input[name="${key}"]`);
}

test("an environment-pinned row renders disabled with its reason, and stays that way", async ({
  page,
}) => {
  // The fixture pins NODUM_LLM_MODEL in the server's environment on purpose.
  await page.goto("/settings");
  const pinned = rowInput(page, "NODUM_LLM_MODEL");
  await expect(pinned).toBeDisabled();
  await expect(pinned).toHaveValue("e2e-pinned-model");
  await expect(page.getByText(/pinned by the environment/i)).toBeVisible();

  // The env-only four are read-only too, each carrying its registry reason.
  const publicUrl = rowInput(page, "NODUM_PUBLIC_URL");
  await expect(publicUrl).toBeDisabled();
  await expect(
    page.getByText(/every capability URL is minted from it/i),
  ).toBeVisible();
});

test("a saved change reports its liveness class and lands in the file", async ({ page }) => {
  await page.goto("/settings");

  // Live class: the provider re-resolves immediately.
  const contextRow = page.locator(".nd-set-row", {
    has: rowInput(page, "NODUM_LLM_CONTEXT_TOKENS"),
  });
  const context = rowInput(page, "NODUM_LLM_CONTEXT_TOKENS");
  await expect(context).toBeEnabled();
  await context.fill("262144");
  await contextRow.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("status").first()).toHaveText("Applied live");

  // Next-run class: the per-run ceilings are read once per run.
  const budgetRow = page.locator(".nd-set-row", {
    has: rowInput(page, "NODUM_LLM_REQUEST_BUDGET"),
  });
  await rowInput(page, "NODUM_LLM_REQUEST_BUDGET").fill("50");
  await budgetRow.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("status").filter({ hasText: /next agent run/ })).toHaveCount(1);
  await expect(page.getByRole("status").filter({ hasText: /mid-cycle/i })).toHaveCount(0);

  // The value survives a reload — it is in the file, not just in the page.
  await page.reload();
  await expect(rowInput(page, "NODUM_LLM_CONTEXT_TOKENS")).toHaveValue("262144");
});

test("one-click revert restores the previous value from the last settings event", async ({
  page,
}) => {
  await page.goto("/settings");
  const thinking = rowInput(page, "NODUM_LLM_THINKING");

  // Two writes: the first has nothing to revert to but a removal, the second
  // names a concrete previous value.
  const thinkingRow = page.locator(".nd-set-row", { has: thinking });
  await thinking.fill("low");
  await thinkingRow.getByRole("button", { name: "Save" }).click();
  // Wait for the write to land (the status note only appears once it has) —
  // the input's value alone also matches the draft before the save resolves.
  await expect(thinkingRow.getByRole("status")).toHaveText("Applied live");
  await expect(thinking).toHaveValue("low");

  await thinking.fill("high");
  await thinkingRow.getByRole("button", { name: "Save" }).click();
  await expect(thinkingRow.getByRole("status")).toHaveText("Applied live");
  await expect(thinking).toHaveValue("high");

  await thinkingRow.getByRole("button", { name: "Revert" }).click();
  await expect(thinking).toHaveValue("low");
});

test("adopt-from-environment previews the candidate and flips the stored flag", async ({
  page,
}) => {
  await page.goto("/settings");
  await expect(
    page.getByRole("button", { name: /adopt from environment \(1\)/i }),
  ).toBeVisible();

  await page.getByRole("button", { name: /adopt from environment/i }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("NODUM_LLM_MODEL");
  // The preview shows the environment's own value.
  await expect(dialog).toContainText("e2e-pinned-model");

  await dialog.getByRole("button", { name: /^Adopt 1$/ }).click();
  await expect(page.getByText(/adopted 1 setting/i)).toBeVisible();

  // Provenance stays `environment` — adopt never touches the host — but the
  // file now carries the key, and the row says so.
  const modelRow = page.locator(".nd-set-row", { has: rowInput(page, "NODUM_LLM_MODEL") });
  await expect(modelRow).toContainText("stored in settings.env");
  await expect(modelRow.locator(".nd-badge--provenance-environment")).toBeVisible();
});

test("the gardener group links the real kill switch and does not overpromise", async ({
  page,
}) => {
  await page.goto("/settings");
  const copy = page.getByText(/does not stop a cycle already spending/i);
  await expect(copy).toBeVisible();
  await copy.getByRole("link", { name: "Journal" }).click();
  await expect(page).toHaveURL(/\/journal$/);
});

test("an embedding-model change confirms the blinded-chunks consequence before the write", async ({
  page,
}) => {
  await page.goto("/settings");
  const modelRow = page.locator(".nd-set-row", {
    has: rowInput(page, "NODUM_EMBED_MODEL"),
  });
  const model = rowInput(page, "NODUM_EMBED_MODEL");
  await expect(model).toBeEnabled();
  await model.fill("e2e-switched-model");
  await modelRow.getByRole("button", { name: "Save" }).click();

  // The write is held: the confirm names the consequence before anything
  // lands. The exact sentence depends on whether the fixture store holds any
  // chunks (it does not on CI — no embeddings extra, no cached model), so the
  // assertion covers both the count-named and the zero-chunk variants; the
  // count-specific copy is pinned by the Vitest unit tests.
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByText(/invisible to search|blinds nothing until the first projection/i),
  ).toBeVisible();
  await expect(dialog.getByText(/re-embeds nothing by itself/i)).toBeVisible();
  await expect(page.getByRole("button", { name: "Change model" })).toBeVisible();

  await dialog.getByRole("button", { name: "Change model" }).click();
  await expect(modelRow.getByRole("status")).toHaveText("Applied live");
  await page.reload();
  await expect(model).toHaveValue("e2e-switched-model");
});

test("the embedding-model row stays editable and the download gate states its cost", async ({
  page,
}) => {
  await page.goto("/settings");
  await expect(rowInput(page, "NODUM_EMBED_MODEL")).toBeEnabled();
  await expect(rowInput(page, "NODUM_EMBED_DOWNLOAD")).toBeEnabled();
  await expect(page.getByText(/0\.2 GB/i)).toBeVisible();
  await expect(page.getByText(/never downloads implicitly/i)).toBeVisible();
});
