import { defineConfig } from "vitest/config";

/**
 * The unit harness for `web/`.
 *
 * Deliberately separate from `vite.config.ts`: the build config carries the
 * React plugin and the `../nodum/_web` output directory, none of which a test
 * run needs, and keeping them apart means a test setting can never change what
 * ships in the wheel.
 *
 * Scope is **pure modules only** — no DOM, no component rendering, no
 * `@testing-library`. Everything under test here is a plain function over plain
 * data; a component harness is a much heavier dependency set and would be a
 * separate decision.
 *
 * Both test suffixes are collected, `.test.ts` and `.test.tsx` — the latter
 * only so a pure helper that happens to live in a `.tsx` file (a component
 * file that also exports a plain function) can be tested where it sits. It is
 * not an opening for component tests: the no-DOM scope above binds a `.tsx`
 * test exactly as it binds a `.ts` one, and the convention stays "extract
 * pure logic to a `.ts` module" when the helper is worth more than one test
 * (`src/views/search/snippetTerms.ts` is the most recent such extraction).
 */

/**
 * The timezone every test runs in.
 *
 * `src/lib/time.ts` exists because SQLite writes `datetime('now')` as UTC with
 * no zone marker and `new Date(s)` reads that as *local* time. **That bug is
 * invisible in UTC** — the wrong answer and the right answer are the same
 * instant — and CI runs in UTC, so an ambient-timezone test run would be a
 * tautology that passes while the code is broken.
 *
 * Kathmandu is chosen for two properties: the offset is +05:45, so an
 * hours-only assumption is caught as well as a zone-less one, and Nepal has no
 * DST, so the expected instant is the same in July and January.
 *
 * `time.test.ts` asserts this actually took effect rather than trusting it.
 */
const TEST_TZ = "Asia/Kathmandu";

export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
    env: { TZ: TEST_TZ },
  },
});
