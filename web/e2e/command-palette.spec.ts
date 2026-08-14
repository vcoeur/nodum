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

async function openPalette(page: Page, modifier: "Control" | "Meta" = "Control"): Promise<void> {
  await expect(page.getByRole("button", { name: "Log out" })).toBeVisible();
  await page.keyboard.press(`${modifier}+K`);
  await expect(page.getByRole("dialog", { name: "Command palette" })).toBeVisible();
}

async function recentStorageKey(page: Page): Promise<string> {
  return await page.evaluate(async () => {
    const human = (await fetch("/api/me")).json() as Promise<{ id: string }>;
    return `nodum.recent-nodes.${encodeURIComponent((await human).id)}`;
  });
}

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test("the pending and expired session gates never expose the palette or stored recents", async ({ page }) => {
  await page.addInitScript(() => {
    window.localStorage.setItem(
      "nodum.recent-nodes.human-owner",
      JSON.stringify([{ id: "alpha", title: "Private read" }]),
    );
  }
  );
  await page.route("**/api/me", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 250));
    await route.continue();
  });
  await page.goto("/");
  await page.keyboard.press("Control+K");
  await expect(page.getByRole("dialog", { name: "Command palette" })).toBeHidden();
  await expect(page.getByText("Private read")).toBeHidden();

  await page.context().clearCookies();
  await page.goto("/search");
  await expect(page.locator('input[name="username"]')).toBeVisible();
  await page.keyboard.press("Control+K");
  await expect(page.getByRole("dialog", { name: "Command palette" })).toBeHidden();
});

test("a non-401 identity failure never selects a prior human's scoped recents", async ({ page }) => {
  const ownerKey = await recentStorageKey(page);
  await page.evaluate((key) => {
    window.localStorage.setItem(key, JSON.stringify([{ id: "alpha", title: "Private read" }]));
  }, ownerKey);
  await page.route("**/api/me", (route) => route.fulfill({ status: 503, body: "unavailable" }));

  await page.goto("/search");

  await expect(page.getByRole("searchbox", { name: "Search the graph" })).toBeVisible();
  await page.keyboard.press("Control+K");
  await expect(page.getByRole("dialog", { name: "Command palette" })).toBeHidden();
  await expect(page.getByText("Private read")).toBeHidden();
});

test("Ctrl-K opens once from search, owns focus, and Escape returns it", async ({ page }) => {
  await page.goto("/search");
  const search = page.locator('input[name="q"]');
  await search.focus();

  await openPalette(page);
  const palette = page.getByRole("dialog", { name: "Command palette" });
  await expect(palette.locator('input[name="command"]')).toBeFocused();

  await page.keyboard.press("Escape");
  await expect(palette).toBeHidden();
  await expect(search).toBeFocused();

  await search.blur();
  await page.keyboard.press("/");
  await expect(search).toBeFocused();
});

test("Meta-K opens on another view and an outside press dismisses it", async ({ page }) => {
  await page.goto("/graph");
  const opener = page.getByRole("link", { name: "Graph", exact: true });
  await opener.focus();

  await openPalette(page, "Meta");
  await page.locator("body").click({ position: { x: 5, y: 5 } });
  await expect(page.getByRole("dialog", { name: "Command palette" })).toBeHidden();
  await expect(opener).toBeFocused();
});

test("palette arrows and Enter navigate without the search Ctrl-K handler racing", async ({ page }) => {
  await page.goto("/search");
  await page.locator('input[name="q"]').waitFor({ state: "visible" });
  await openPalette(page);
  const palette = page.getByRole("dialog", { name: "Command palette" });

  await page.locator('input[name="command"]').fill("new");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/editor$/);
  await expect(palette).toBeHidden();
});

test("the visible empty palette keeps its combobox expanded and names the no-match state", async ({ page }) => {
  await page.goto("/search");
  await openPalette(page);
  const input = page.locator('input[name="command"]');
  await input.fill("zzz-unmatched");

  await expect(input).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByRole("listbox", { name: "Commands and nodes" })).toBeVisible();
  await expect(page.getByText("No commands or nodes match that search.", { exact: true })).toBeVisible();
  await expect(page.getByRole("option")).toHaveCount(0);
});

test("a superseded node lookup cannot remain visible or execute", async ({ page }) => {
  await page.goto("/search");
  await page.locator('input[name="q"]').waitFor({ state: "visible" });
  await page.route("**/api/links/suggest?prefix=first&limit=8", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 300));
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({ nodes: [{ id: "alpha", title: "Alpha node" }] }) });
  });
  await openPalette(page);
  const input = page.locator('input[name="command"]');
  await input.fill("first");
  await input.fill("second");
  await page.waitForTimeout(350);
  await expect(page.getByRole("option", { name: /Alpha node/ })).toBeHidden();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/search$/);
});

test("Ctrl-K does not stack the palette over another modal", async ({ page }) => {
  await page.goto(`/node/title/${encodeURIComponent("Alpha node")}`);
  await expect(page.getByRole("heading", { name: "Alpha node" })).toBeVisible();
  await page.getByRole("button", { name: "Archive" }).click();
  await expect(page.getByRole("dialog", { name: /Archive/ })).toBeVisible();
  await page.keyboard.press("Control+K");
  await expect(page.getByRole("dialog", { name: "Command palette" })).toBeHidden();
  const archiveDialog = page.getByRole("dialog", { name: /Archive/ });
  await expect.poll(() => archiveDialog.evaluate((dialog) => dialog.contains(document.activeElement))).toBe(true);
});

test("slash does not focus search behind an archive dialog", async ({ page }) => {
  await page.goto(`/node/title/${encodeURIComponent("Alpha node")}`);
  await page.getByRole("button", { name: "Archive" }).click();
  const archiveDialog = page.getByRole("dialog", { name: /Archive/ });
  await expect(archiveDialog).toBeVisible();

  await page.keyboard.press("/");
  await expect.poll(() => archiveDialog.evaluate((dialog) => dialog.contains(document.activeElement))).toBe(true);
});

test("Ctrl-K does not stack the palette over the asset lightbox", async ({ page }) => {
  await page.goto("/assets");
  await page.getByRole("button", { name: /Open fixture\.txt/ }).click();
  const lightbox = page.getByRole("dialog", { name: "fixture.txt" });
  await expect(lightbox).toBeVisible();

  await page.keyboard.press("Control+K");
  await expect(page.getByRole("dialog", { name: "Command palette" })).toBeHidden();
  await expect.poll(() => lightbox.evaluate((dialog) => dialog.contains(document.activeElement))).toBe(true);
});

test("slash does not focus search behind the asset lightbox", async ({ page }) => {
  await page.goto("/assets");
  await page.getByRole("button", { name: /Open fixture\.txt/ }).click();
  const lightbox = page.getByRole("dialog", { name: "fixture.txt" });
  await expect(lightbox).toBeVisible();

  await page.keyboard.press("/");
  await expect.poll(() => lightbox.evaluate((dialog) => dialog.contains(document.activeElement))).toBe(true);
});

test("the asset lightbox restores its tile opener on Escape and backdrop dismissal", async ({ page }) => {
  await page.goto("/assets");
  const opener = page.getByRole("button", { name: /Open fixture\.txt/ });
  await opener.focus();
  await opener.click();
  const lightbox = page.getByRole("dialog", { name: "fixture.txt" });
  await expect(lightbox).toBeVisible();

  await page.keyboard.press("Escape");
  await expect(lightbox).toBeHidden();
  await expect(opener).toBeFocused();

  await opener.click();
  await expect(lightbox).toBeVisible();
  await page.locator(".nd-lightbox-backdrop").click({ position: { x: 5, y: 5 } });
  await expect(lightbox).toBeHidden();
  await expect(opener).toBeFocused();
});

test("an account transition changes recent scope before the second human can render it", async ({ page }) => {
  await page.goto(`/node/title/${encodeURIComponent("Alpha node")}`);
  await expect(page.getByRole("heading", { name: "Alpha node" })).toBeVisible();
  const ownerKey = await recentStorageKey(page);
  await page.evaluate(() => window.localStorage.setItem("nodum.write-target", "private-space"));

  await page.goto("/login");
  await page.locator('input[name="username"]').fill("second");
  await page.locator('input[name="current-password"]').fill(PASSWORD);
  await page.locator('button[type="submit"]').click();
  await expect(page.locator('[title="Signed in as second"]')).toBeVisible();
  await page.goto("/search");
  await expect(page.getByText("Recent reads")).toBeHidden();
  await openPalette(page);
  await expect(page.getByRole("option", { name: /Alpha node/ })).toBeHidden();
  await page.keyboard.press("Escape");
  await expect
    .poll(() =>
      page.evaluate((key) => ({
        ownerRecents: window.localStorage.getItem(key),
        writeTarget: window.localStorage.getItem("nodum.write-target"),
      }), ownerKey),
    )
    .toEqual({ ownerRecents: expect.stringContaining("Alpha node"), writeTarget: null });
});

test("a successful node read becomes a persisted recent, while a stale recent stays honest", async ({ page }) => {
  await page.goto(`/node/title/${encodeURIComponent("Alpha node")}`);
  await expect(page.getByRole("heading", { name: "Alpha node" })).toBeVisible();

  await page.goto("/search");
  await expect(page.getByText("Recent reads")).toBeVisible();
  await expect(page.getByRole("link", { name: "Alpha node" })).toBeVisible();

  await page.reload();
  await expect(page.getByText("Recent reads")).toBeVisible();
  await openPalette(page);
  await expect(page.getByRole("option", { name: /Alpha node/ })).toBeVisible();
});

test("a stale recent names its uncertainty and opens the reader's honest failure", async ({ page }) => {
  const ownerKey = await recentStorageKey(page);
  await page.goto("/search");
  await page.evaluate(
    (key) => window.localStorage.setItem(key, JSON.stringify([{ id: "stale-node", title: "Retired read" }])),
    ownerKey,
  );
  await page.reload();
  await expect(page.getByText("Previously opened entries may no longer be available.")).toBeVisible();
  await page.getByRole("link", { name: "Retired read" }).click();
  await expect(page.getByText("this node", { exact: false })).toBeVisible();
});

test("cycle rehearsal sends dry_run true and never offers a live-cycle command", async ({ page }) => {
  await page.goto("/search");
  await page.locator('input[name="q"]').waitFor({ state: "visible" });
  await openPalette(page);
  await page.locator('input[name="command"]').fill("cycle");
  const rehearsal = page.waitForRequest((request) => request.url().endsWith("/api/cycles"));
  await page.keyboard.press("Enter");
  expect(JSON.parse((await rehearsal).postData() ?? "{}")).toEqual({ dry_run: true });
});

test("an ArrowDown pressed before lookup results cannot strand Enter", async ({ page }) => {
  await page.goto("/search");
  await page.locator('input[name="q"]').waitFor({ state: "visible" });
  await page.route("**/api/links/suggest?prefix=alpha&limit=8", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 300));
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ nodes: [{ id: "alpha", title: "Alpha node" }] }),
    });
  });
  await openPalette(page);
  const input = page.locator('input[name="command"]');
  await input.fill("alpha");
  // The lookup is still pending, so the list is empty and the selection index
  // goes to -1. When the result lands, the clamp must recover the first row;
  // a stuck -1 would leave Enter unable to activate the visible option.
  await page.keyboard.press("ArrowDown");
  await page.waitForTimeout(350);
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/node\/alpha/);
  await expect(page.getByRole("dialog", { name: "Command palette" })).toBeHidden();
});

test("a superseded reader load cannot record a recent after navigation", async ({ page }) => {
  const ownerKey = await recentStorageKey(page);
  await page.route("**/api/nodes/stale-read?depth=1", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 300));
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        nodes: [{ id: "stale-read", title: "Stale read", type: "concept" }],
      }),
    });
  });
  await page.goto("/node/stale-read");
  await page.goto("/search");
  await page.waitForTimeout(350);
  expect(await page.evaluate((key) => window.localStorage.getItem(key), ownerKey)).toBeNull();
});

test("a reload and another verified tab expose only their shared human scope", async ({ page, context }) => {
  const secondTab = await context.newPage();
  await secondTab.goto("/search");
  await expect(secondTab.getByText("Recent reads")).toBeHidden();

  await page.goto(`/node/title/${encodeURIComponent("Alpha node")}`);
  await expect(page.getByRole("heading", { name: "Alpha node" })).toBeVisible();

  await expect(secondTab.getByText("Recent reads")).toBeVisible();
  await openPalette(secondTab);
  await expect(secondTab.getByRole("option", { name: /Alpha node/ })).toBeVisible();
  await secondTab.close();
});

test("an account transition in another tab removes the old verified scope before it can render", async ({ page, context }) => {
  await page.goto(`/node/title/${encodeURIComponent("Alpha node")}`);
  await expect(page.getByRole("heading", { name: "Alpha node" })).toBeVisible();
  const secondTab = await context.newPage();
  await secondTab.goto("/search");
  await expect(secondTab.getByText("Recent reads")).toBeVisible();

  await page.goto("/login");
  await page.locator('input[name="username"]').fill("second");
  await page.locator('input[name="current-password"]').fill(PASSWORD);
  await page.locator('button[type="submit"]').click();
  await expect(page.locator('[title="Signed in as second"]')).toBeVisible();

  await expect(secondTab.getByText("Recent reads")).toBeHidden();
  await secondTab.close();
});

test("another tab's session transition wins over a still-pending identity response", async ({ page, context }) => {
  // Tab B verifies as owner and records a recent read.
  await page.goto(`/node/title/${encodeURIComponent("Alpha node")}`);
  await expect(page.getByRole("heading", { name: "Alpha node" })).toBeVisible();
  await page.goto("/search");
  await expect(page.getByText("Recent reads")).toBeVisible();

  // Hold the first /api/me after reload (issued with the owner cookie) until
  // the test releases it: it must land only after the session has changed
  // owners, which is the exact race the identity re-verification exists to win.
  let releaseStale: () => void;
  const staleHeld = new Promise<void>((resolve) => {
    releaseStale = resolve;
  });
  let firstIdentityRequest = true;
  await page.route("**/api/me", async (route) => {
    if (firstIdentityRequest) {
      firstIdentityRequest = false;
      await staleHeld;
    }
    await route.continue();
  });
  await page.reload();

  // Tab A logs in as the second human while tab B's identity check is pending.
  const tabA = await context.newPage();
  await tabA.goto("/login");
  await tabA.locator('input[name="username"]').fill("second");
  await tabA.locator('input[name="current-password"]').fill(PASSWORD);
  await tabA.locator('button[type="submit"]').click();
  await expect(tabA.locator('[title="Signed in as second"]')).toBeVisible();

  // The re-verification completes with the second human's session: the header
  // names them and no prior titles are visible.
  await expect(page.locator('[title="Signed in as second"]')).toBeVisible();
  await expect(page.getByText("Recent reads")).toBeHidden();

  // The stale owner response finally arrives. It must not restore the owner's
  // titles: the session cookie this tab now carries belongs to the second human.
  releaseStale();
  await expect(page.getByText("Recent reads")).toBeHidden();
  await page.keyboard.press("Control+K");
  await expect(page.getByRole("dialog", { name: "Command palette" })).toBeVisible();
  await expect(page.getByRole("option", { name: /Alpha node/ })).toBeHidden();
  await page.keyboard.press("Escape");
});

test("logout drops the active scope, and a later verified identity restores only its own recents", async ({ page }) => {
  await page.goto(`/node/title/${encodeURIComponent("Alpha node")}`);
  await expect(page.getByRole("heading", { name: "Alpha node" })).toBeVisible();
  await page.getByRole("button", { name: "Log out" }).click();
  await expect(page.locator('input[name="username"]')).toBeVisible();

  await signIn(page);
  await page.goto("/search");
  await expect(page.getByText("Recent reads")).toBeVisible();
  await expect(page.getByRole("link", { name: "Alpha node" })).toBeVisible();
});

test("the palette remains internally scrollable at a narrow viewport", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 360 });
  await page.goto("/search");
  await page.locator('input[name="q"]').waitFor({ state: "visible" });
  await openPalette(page);
  const palette = page.getByRole("dialog", { name: "Command palette" });
  await expect(palette.locator('input[name="command"]')).toHaveAttribute("aria-expanded", "true");
  await expect(palette.locator(".nd-palette__list")).toHaveCSS("overflow-y", "auto");
  await page.keyboard.press("Tab");
  await expect(palette.getByRole("button", { name: "Close" })).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(palette.locator('input[name="command"]')).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowDown");
  await expect.poll(() => palette.locator(".nd-palette__list").evaluate((list) => list.scrollTop)).toBeGreaterThan(0);
});
