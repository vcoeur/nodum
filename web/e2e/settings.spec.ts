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

/**
 * The select of one settings row — a row whose key accepts a closed set.
 *
 * Separate from {@link rowInput} rather than a union locator: the two are
 * driven differently (`fill` against a text box, `selectOption` against a
 * select), so a helper that returned either would only move the branch to
 * every call site.
 */
function rowSelect(page: Page, key: string) {
  return page.locator(`select[name="${key}"]`);
}

/**
 * Open a row's info popup the way a reader does: scroll the button into view,
 * let the scroll settle, then click.
 *
 * The settle is not cosmetic. Playwright's click scrolls a below-fold button
 * into view and the browser commits that scroll's event a few milliseconds
 * *after* the click dispatch — and the popover closes on scroll, the shared
 * transient-overlay rule (`lib/dismissWatchers.ts`). A click landing inside
 * that window opens and instantly dismisses, which is a test artefact, not a
 * behaviour a reader can produce: a reader scrolls, the page settles, then
 * they click. This helper models that.
 */
async function openInfoPopup(page: Page, key: string): Promise<void> {
  const opener = page.getByRole("button", { name: `About ${key}` });
  await opener.scrollIntoViewIfNeeded();
  await page.waitForTimeout(150);
  await opener.click();
  await expect(page.getByRole("dialog", { name: `About ${key}` })).toBeVisible();
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
  // A reasoning level is a closed set, so this row is a select — the page
  // cannot offer a level the server's validator would refuse.
  const thinking = rowSelect(page, "NODUM_LLM_THINKING");

  // Two writes: the first has nothing to revert to but a removal, the second
  // names a concrete previous value.
  const thinkingRow = page.locator(".nd-set-row", { has: thinking });
  await thinking.selectOption("low");
  await thinkingRow.getByRole("button", { name: "Save" }).click();
  // Wait for the write to land (the status note only appears once it has) —
  // the control's value alone also matches the draft before the save resolves.
  await expect(thinkingRow.getByRole("status")).toHaveText("Applied live");
  await expect(thinking).toHaveValue("low");

  await thinking.selectOption("high");
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

test("the settings content column matches the Admin page's centred column", async ({ page }) => {
  // The root used to carry `nd-view--wide` (max-width: none, padding: 0 —
  // the full-bleed canvas variant), which sent the rows to x≈6 px with
  // fragments clipped at the viewport edge. v0.24.0 re-asserted a gutter over
  // it; the centred-column branch drops `--wide` entirely, so the page gets
  // the shared centred column (`nd-view`: max-width 68rem, margin auto) the
  // Admin page has. Pin the geometry with assertions that hold in any
  // scrollbar environment: same column width as Admin, both columns centred
  // in the viewport (so a full-bleed root — which would sit at x=0 — fails),
  // and a real gutter on the left.
  await page.goto("/settings");
  const settingsColumn = page.locator(".nd-view.nd-set");
  await expect(settingsColumn).toBeVisible();
  const settingsBox = await settingsColumn.boundingBox();
  expect(settingsBox).not.toBeNull();
  // Each page's own layout viewport, read while that page is on screen.
  const settingsClientWidth = await page.evaluate(
    () => document.documentElement.clientWidth,
  );

  await page.goto("/admin");
  const adminColumn = page.locator(".nd-view.nd-ad");
  await expect(adminColumn).toBeVisible();
  const adminBox = await adminColumn.boundingBox();
  expect(adminBox).not.toBeNull();
  const adminClientWidth = await page.evaluate(
    () => document.documentElement.clientWidth,
  );

  expect(settingsBox!.width).toBe(adminBox!.width);
  // The old gutter regression: rows used to start at x≈6 px with fragments
  // clipped at the viewport edge.
  expect(settingsBox!.x).toBeGreaterThanOrEqual(20);

  // Both pages centre their column; a full-bleed root would sit at x=0. The
  // expected centre is computed from each page's own layout viewport
  // (`clientWidth`, which excludes a classic scrollbar when one is present)
  // so the assertion holds whatever the pages' scroll state.
  expect(settingsBox!.x).toBeCloseTo((settingsClientWidth - settingsBox!.width) / 2, 0);
  expect(adminBox!.x).toBeCloseTo((adminClientWidth - adminBox!.width) / 2, 0);
});

test("every settings group and the Export section render as a shared nd-card", async ({ page }) => {
  // The card is the shared primitive, not a settings look-alike: the same
  // `.nd-card` class the Admin page's AgentCard uses, with the primitive's
  // border-radius and surface background applied.
  await page.goto("/settings");
  for (const title of [
    "Model",
    "Gardener",
    "Requests",
    "Audio",
    "Embeddings",
    "Server",
    "Export",
  ]) {
    const card = page.locator(".nd-card", {
      has: page.getByRole("heading", { name: title, level: 2 }),
    });
    await expect(card).toBeVisible();
    await expect(card).toHaveCSS("border-radius", "10px");
    await expect(card).toHaveCSS("background-color", "rgb(23, 27, 33)");
  }
});

test("an info popup explains a row from the registry's own copy", async ({ page }) => {
  await page.goto("/settings");
  await openInfoPopup(page, "NODUM_EMBED_MODEL");
  const popover = page.getByRole("dialog", { name: "About NODUM_EMBED_MODEL" });
  // The summary, the longer help, the default, and the liveness class — all
  // from the row the server serialised.
  await expect(popover).toContainText("The local embedding model");
  await expect(popover).toContainText("Each stored chunk records the model that embedded it");
  await expect(popover).toContainText("sentence-transformers/paraphrase");
  await expect(popover).toContainText("Applied live");
});

test("the embed-download popup and the row note say the same sentence", async ({ page }) => {
  // One fact, one home: the registry's help for NODUM_EMBED_DOWNLOAD is the
  // page's own EMBED_DOWNLOAD_NOTE word for word, and both are on screen.
  await page.goto("/settings");
  const row = page.locator(".nd-set-row", { has: rowInput(page, "NODUM_EMBED_DOWNLOAD") });
  const note = row.locator(".nd-set-row__note");
  await openInfoPopup(page, "NODUM_EMBED_DOWNLOAD");
  const popover = page.getByRole("dialog", { name: "About NODUM_EMBED_DOWNLOAD" });
  await expect(popover).toContainText("0.2 GB");
  await expect(popover).toContainText("never downloads implicitly");
  expect((await popover.locator(".nd-set-info-popover__help").textContent())?.trim()).toBe(
    (await note.textContent())?.trim(),
  );
});

test("a pinned row's popup states no liveness, because this page cannot change it", async ({
  page,
}) => {
  // The fixture pins NODUM_LLM_MODEL in the environment. "Applied live" would
  // be a claim about a change this page cannot make, so the popup withholds
  // the liveness line entirely.
  await page.goto("/settings");
  await openInfoPopup(page, "NODUM_LLM_MODEL");
  const popover = page.getByRole("dialog", { name: "About NODUM_LLM_MODEL" });
  await expect(popover).toContainText("The model name; unset means no provider");
  await expect(popover).toContainText("There is no default model");
  await expect(popover).not.toContainText("Takes effect");
  await expect(popover).not.toContainText("Applied live");
});

test("an info popup opens, takes focus, and hands it back on Escape", async ({ page }) => {
  await page.goto("/settings");
  const opener = page.getByRole("button", { name: "About NODUM_EMBED_MODEL" });
  await openInfoPopup(page, "NODUM_EMBED_MODEL");
  const popover = page.getByRole("dialog", { name: "About NODUM_EMBED_MODEL" });
  // The panel takes focus itself on mount, like the context menu — a popup
  // that paints without taking focus is unreachable by keyboard.
  await expect
    .poll(() =>
      popover.evaluate(
        (panel) =>
          panel === document.activeElement || panel.contains(document.activeElement),
      ),
    )
    .toBe(true);

  await page.keyboard.press("Escape");
  await expect(popover).toBeHidden();
  // The hand-back is the whole reason the restore exists; landing on <body>
  // is the failure it prevents.
  await expect(opener).toBeFocused();
});

test("a press outside dismisses the info popup", async ({ page }) => {
  await page.goto("/settings");
  await openInfoPopup(page, "NODUM_EMBED_MODEL");
  const popover = page.getByRole("dialog", { name: "About NODUM_EMBED_MODEL" });

  await page.getByRole("heading", { name: "Settings" }).click();
  await expect(popover).toBeHidden();
});

test("a document shortcut still reaches the app behind an open info popup", async ({ page }) => {
  await page.goto("/settings");
  await openInfoPopup(page, "NODUM_EMBED_MODEL");
  const popover = page.getByRole("dialog", { name: "About NODUM_EMBED_MODEL" });

  // The panel owns Escape and nothing else, so Ctrl/Cmd-K — the app's own
  // command palette — still lands. The palette is a Modal that takes focus
  // programmatically, so the popover survives behind it (the same way the
  // context menu does) and stays dismissible once the palette is gone.
  //
  // Named, not the bare role: this page carries two `<select>` elements — the
  // endpoint select and the reasoning level — and both are comboboxes too, so
  // an unqualified `getByRole("combobox")` is a strict-mode violation rather
  // than a reference to the palette.
  const palette = page.getByRole("combobox", { name: "Find a command or node" });
  await page.keyboard.press("Control+K");
  await expect(palette).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(palette).toBeHidden();
  await expect(popover).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(popover).toBeHidden();
});

test("the endpoint select offers the deployment's menu and stores the label it picks", async ({
  page,
}) => {
  await page.goto("/settings");
  const endpoint = rowSelect(page, "NODUM_LLM_ENDPOINT");
  const endpointRow = page.locator(".nd-set-row", { has: endpoint });

  // The fixture narrows NODUM_LLM_ENDPOINTS to two of the shipped four, so
  // this is the allow-list being honoured rather than the registry being
  // listed: `local` and `openrouter` are shipped and must not be offered.
  await expect(endpoint.locator("option")).toHaveText(["not set", "DeepSeek", "Kimi (Moonshot)"]);

  // The option shows a title and stores a label — a browser never names a URL.
  await endpoint.selectOption("kimi");
  await endpointRow.getByRole("button", { name: "Save" }).click();
  await expect(endpointRow.getByRole("status")).toHaveText("Applied live");
  await expect(endpoint).toHaveValue("kimi");
  // The row names the layer twice — a provenance badge and the meta line — so
  // this asserts the meta phrasing, which is the unambiguous one.
  await expect(endpointRow).toContainText("stored in settings.env");

  // Each offered endpoint has its own credential row, and the one that is not
  // offered has none — there is no field through which a key could be entered
  // for an endpoint this deployment does not serve.
  await expect(rowInput(page, "NODUM_LLM_KEY_DEEPSEEK")).toBeVisible();
  await expect(rowInput(page, "NODUM_LLM_KEY_KIMI")).toBeVisible();
  await expect(rowInput(page, "NODUM_LLM_KEY_OPENROUTER")).toHaveCount(0);

  // Selecting an endpoint that serves many windows re-writes the context-tokens
  // note, because nodum asserts no window for it and this row is the only place
  // one can come from.
  const context = page.locator(".nd-set-row", { has: rowInput(page, "NODUM_LLM_CONTEXT_TOKENS") });
  await expect(context).toContainText("Kimi's window depends on the model");
});

test("the endpoint menu is read-only in the browser", async ({ page }) => {
  await page.goto("/settings");
  // The allow-list bounds the select, so a page that could edit it could widen
  // its own choices. It renders with its reason, disabled, like the other
  // environment-only names.
  const menu = rowInput(page, "NODUM_LLM_ENDPOINTS");
  await expect(menu).toBeDisabled();
  await expect(menu).toHaveValue("deepseek,kimi");
});
