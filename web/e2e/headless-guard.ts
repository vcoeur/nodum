import type { FullConfig } from "@playwright/test";

/**
 * Playwright `globalSetup` — refuse to open real windows on a live desktop.
 *
 * `playwright.config.ts` states `headless: true`, but a default is not a
 * guarantee: `--headed`, `PWDEBUG=1`, a `use` override in a future project
 * entry, or a change in Playwright's own default all put a browser on whatever
 * display the developer is looking at, stealing focus mid-run. On a workstation
 * that is a nuisance; on a suite that presses Escape and types `/` it is a
 * nuisance aimed at the keyboard.
 *
 * So the headless promise is checked here, before any worker launches a
 * browser, rather than assumed. A visible run is still available — it is just
 * opt-in and deliberate.
 *
 * The same guard exists in condash for the same reason; there it is Electron,
 * which cannot go headless at all and needs Xvfb instead.
 *
 * @param config The resolved Playwright configuration.
 */
export default function headlessGuard(config: FullConfig): void {
  if (process.env.NODUM_E2E_HEADED === "1") return;

  const onLiveDisplay =
    process.platform === "linux" &&
    Boolean(process.env.WAYLAND_DISPLAY || process.env.DISPLAY);
  if (!onLiveDisplay) return;

  const headed = config.projects.some((project) => project.use.headless === false);
  if (!headed) return;

  throw new Error(
    "Refusing to open a browser window on your live display session — this suite " +
      "presses Escape, types `/` and moves focus, so a visible run steals the " +
      "keyboard.\n" +
      "Run it headless (the default: `make web-e2e`), or opt in explicitly with " +
      "`NODUM_E2E_HEADED=1 npx playwright test --headed`.",
  );
}
