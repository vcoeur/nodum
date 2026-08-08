import { defineConfig } from "@playwright/test";

/**
 * End-to-end configuration for the nodum web UI.
 *
 * There is no component/DOM harness in this repo by design, so the browser is
 * the harness for anything React renders. These specs are the durable form of
 * the manual pass `AGENTS.md` requires — they exist because a transient overlay
 * that owns focus cost nine review rounds when nobody ran it.
 *
 * `channel: "chrome"` uses the system Chrome rather than downloading a browser,
 * matching every other Playwright run in this workspace.
 */
const PORT = process.env.NODUM_E2E_PORT ?? "8699";

export default defineConfig({
  testDir: "./e2e",
  testMatch: /.*\.spec\.ts$/,
  fullyParallel: false,
  // One worker: every spec shares the one fixture database, and the archive
  // specs mutate it.
  workers: 1,
  reporter: [["list"]],
  timeout: 30_000,
  expect: { timeout: 10_000 },
  // Retries absorb cold-start races CI sees and a local box does not; local
  // runs stay strict so flake surfaces in the author's terminal.
  retries: process.env.CI ? 2 : 0,
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    // Locally the system Chrome, so nothing is downloaded and the browser is
    // the one the reader actually uses. CI has no system Chrome it can rely on,
    // so it installs Playwright's chromium and this falls through to it.
    channel: process.env.CI ? undefined : "chrome",
    trace: process.env.CI ? "retain-on-failure" : "off",
  },
  webServer: {
    command: "node e2e/serve-fixture.mjs",
    url: `http://127.0.0.1:${PORT}/healthz`,
    // Seeding runs migrations and an argon2id hash before the port opens.
    timeout: 120_000,
    reuseExistingServer: false,
    stdout: "pipe",
    stderr: "pipe",
  },
});
