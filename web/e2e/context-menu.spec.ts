import { expect, test, type Page } from "@playwright/test";

/**
 * The four checks any transient focus-owning overlay owes, run against a real
 * browser.
 *
 * Each one corresponds to a defect that shipped to review and cost a round:
 * focus never handed back stranded a keyboard reader on `<body>`; a toggle that
 * depended on event ordering opened a menu it could not close; Space stopped
 * activating items when the key gate over-reached; and a blanket
 * `stopPropagation` across the portal killed every shortcut on the surface
 * behind. None of them could fail a type check or a unit test, and all four are
 * about a minute of clicking.
 */

const PASSWORD = process.env.NODUM_E2E_PASSWORD ?? "e2e-secret-password";

async function signIn(page: Page): Promise<void> {
  await page.goto("/");
  // The login view is a lazy chunk behind a `GET /api/me` 401, so it is not on
  // the page when `goto` resolves. Waiting for it rather than probing once is
  // the difference between signing in and silently staying anonymous.
  const username = page.locator('input[name="username"]');
  await username.waitFor({ state: "visible" });
  await username.fill("owner");
  await page.locator('input[name="current-password"]').fill(PASSWORD);
  await page.locator('button[type="submit"]').click();
  await expect(username).toBeHidden();
}

/**
 * Open the reading view of the seeded node whose title is given.
 *
 * Via `/node/title/:title` — the wikilink route — rather than through search,
 * so a spec about the menu does not fail when the search projector is the thing
 * that broke.
 */
async function openNode(page: Page, title: string): Promise<void> {
  await page.goto(`/node/title/${encodeURIComponent(title)}`);
  await expect(page.getByRole("heading", { name: title })).toBeVisible();
}

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test("the menu button opens the menu and puts focus inside it", async ({ page }) => {
  await openNode(page, "Alpha node");
  const opener = page.locator(".nd-menu-button").first();
  await opener.click();

  const menu = page.getByRole("menu");
  await expect(menu).toBeVisible();
  // The panel takes focus itself on mount, before any item is highlighted — so
  // this asks whether focus is *at or within* it, not for a focused descendant.
  // A menu that paints without taking focus is unreachable by keyboard, and
  // Escape then drops the reader on <body>.
  await expect
    .poll(() =>
      menu.evaluate(
        (panel) => panel === document.activeElement || panel.contains(document.activeElement),
      ),
    )
    .toBe(true);
});

test("Escape dismisses and hands focus back to the opener", async ({ page }) => {
  await openNode(page, "Alpha node");
  const opener = page.locator(".nd-menu-button").first();
  await opener.click();
  await expect(page.getByRole("menu")).toBeVisible();

  await page.keyboard.press("Escape");
  await expect(page.getByRole("menu")).toBeHidden();
  // The hand-back is the whole reason the restore exists; landing on <body>
  // is the failure it prevents.
  await expect(opener).toBeFocused();
});

test("a press outside dismisses the menu", async ({ page }) => {
  await openNode(page, "Alpha node");
  await page.locator(".nd-menu-button").first().click();
  await expect(page.getByRole("menu")).toBeVisible();

  await page.locator("body").click({ position: { x: 5, y: 5 } });
  await expect(page.getByRole("menu")).toBeHidden();
});

test("arrow keys move within the menu without driving the page behind it", async ({ page }) => {
  await openNode(page, "Alpha node");
  await page.locator(".nd-menu-button").first().click();
  const menu = page.getByRole("menu");
  await expect(menu).toBeVisible();

  const url = page.url();
  await page.keyboard.press("ArrowDown");
  await expect(menu.getByRole("menuitem").first()).toBeFocused();
  await page.keyboard.press("ArrowRight");
  // ArrowRight is a navigation key on the surface behind. A portal bubbles
  // through the React tree, so an un-gated key reached it and navigated away
  // with the menu still painted.
  expect(page.url()).toBe(url);
  await expect(menu).toBeVisible();
});

test("Space activates a menu item", async ({ page }) => {
  await openNode(page, "Alpha node");
  await page.locator(".nd-menu-button").first().click();
  const menu = page.getByRole("menu");
  await page.keyboard.press("ArrowDown");

  const first = menu.getByRole("menuitem").first();
  const label = (await first.textContent())?.trim() ?? "";
  await page.keyboard.press(" ");
  // Space is the ARIA activation key on a focused menuitem. Preventing it
  // wholesale made every action unreachable by keyboard.
  await expect(menu).toBeHidden();
  expect(label.length).toBeGreaterThan(0);
});

test("a document shortcut still reaches the surface behind an open menu", async ({ page }) => {
  // On the search view, where the shortcut lives and where the regression was.
  await page.goto("/search");
  const query = page.locator('input[name="q"]');
  await query.waitFor({ state: "visible" });
  await query.fill("Alpha");

  const opener = page.locator(".nd-menu-button").first();
  await opener.waitFor({ state: "visible" });
  await opener.click();
  await expect(page.getByRole("menu")).toBeVisible();

  // A React portal bubbles through the React tree, so a blanket
  // stopPropagation on the panel took out every document-level shortcut while
  // a menu was open — `/` and Ctrl-K here, Escape and the Tab trap in Modal.
  // The menu owns its own key vocabulary and nothing else; `/` belongs to the
  // surface behind, and focus leaving the panel is itself a dismissal.
  await page.keyboard.press("/");
  await expect(query).toBeFocused();
  await expect(page.getByRole("menu")).toBeHidden();
});
