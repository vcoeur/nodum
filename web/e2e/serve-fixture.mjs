#!/usr/bin/env node
/**
 * Seed a throwaway graph and serve it, for the end-to-end suite.
 *
 * Playwright's `webServer` runs one command, so seeding and serving are one
 * script: build a database in a temp directory, give the seeded `owner` a
 * password the specs know, create the handful of nodes the specs act on, then
 * hand the process over to `nodum serve`.
 *
 * The database is temporary and per-run. A suite that right-clicks a node and
 * archives it must not be pointed at anything a person cares about, and a fresh
 * graph is also the only way the specs can assert on exact counts.
 */
import { execFileSync, spawn } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const PORT = process.env.NODUM_E2E_PORT ?? '8699';
const PASSWORD = process.env.NODUM_E2E_PASSWORD ?? 'e2e-secret-password';
const CUSTOM_BASE_URL = process.env.NODUM_E2E_LLM_BASE_URL ?? '';
const REPO_ROOT = new URL('../../', import.meta.url).pathname;

const dbDir = mkdtempSync(join(tmpdir(), 'nodum-e2e-'));
const dbPath = join(dbDir, 'graph.db');

/** Run a nodum verb against the fixture database, failing loudly. */
function nodum(args, { stdin } = {}) {
  return execFileSync('uv', ['run', 'nodum', ...args], {
    cwd: REPO_ROOT,
    env: { ...process.env, NODUM_DB: dbPath },
    input: stdin,
    encoding: 'utf8',
    stdio: stdin === undefined ? ['ignore', 'pipe', 'inherit'] : ['pipe', 'pipe', 'inherit'],
  });
}

// Build first, always. `nodum serve` serves the bundle in `nodum/_web/`, not
// `web/src` — so without this the suite happily tests whatever was built last,
// which during development is the code before the change under test. A green
// run against a stale bundle is worse than no run.
execFileSync('make', ['web-build'], { cwd: REPO_ROOT, stdio: 'inherit' });

nodum(['init']);
// `owner` is seeded by the migration; it is passwordless until this runs, and
// the web UI has no other way in.
nodum(['human', 'passwd', 'owner', '--as', 'owner'], { stdin: `${PASSWORD}\n${PASSWORD}\n` });
const second = JSON.parse(nodum(['human', 'create', 'second', '--as', 'owner']));
nodum(['human', 'passwd', second.id, '--as', 'owner', '--password', PASSWORD]);

// Two nodes and an edge between them: enough for a reading view with a
// populated edge rail, which is where the contextual actions live.
const alpha = JSON.parse(
  nodum(['node', 'create', '--type', 'concept', '--title', 'Alpha node', '--as', 'owner']),
);
const beta = JSON.parse(
  nodum(['node', 'create', '--type', 'concept', '--title', 'Beta node', '--as', 'owner']),
);
nodum(['edge', 'create', alpha.id, beta.id, '--type', 'relates_to', '--as', 'owner']);
// A non-image asset exercises the typed lightbox without a rendition dependency.
const assetPath = join(dbDir, 'fixture.txt');
writeFileSync(assetPath, 'fixture asset\n');
nodum(['asset', 'register', assetPath]);
const archivedSpace = JSON.parse(
  nodum(['space-create', 'Retired research', '--as', 'owner']),
);
nodum(['space-archive', archivedSpace.id, '--as', 'owner']);

// Specs read these rather than hardcoding ids the seed might renumber.
process.stdout.write(
  `${JSON.stringify({ alpha: alpha.id, beta: beta.id, archivedSpace: archivedSpace.id, dbPath })}\n`,
);

// NODUM_LLM_MODEL is pinned deliberately: it gives the Settings specs a row
// the page must render disabled ("pinned by the environment") and an
// adopt-from-environment candidate, against a live 409-backed server.
//
// NODUM_LLM_ENDPOINTS is narrowed deliberately too, and to a *proper subset*:
// it gives the endpoint spec a menu that is demonstrably the deployment's
// rather than the whole shipped registry, so a select that ignored the
// allow-list would fail rather than pass by coincidence.
//
// The default fixture explicitly clears NODUM_LLM_BASE_URL after inheriting
// the environment. A developer's shell must not silently turn ordinary
// Settings specs into the custom-endpoint state. The override scenario opts
// in through the E2E-only name, so the browser suite can cover it separately.
const server = spawn(
  'uv',
  ['run', 'nodum', 'serve', '--db', dbPath, '--port', PORT],
  {
    cwd: REPO_ROOT,
    env: {
      ...process.env,
      NODUM_LLM_MODEL: 'e2e-pinned-model',
      NODUM_LLM_ENDPOINTS: 'deepseek,kimi',
      NODUM_LLM_BASE_URL: CUSTOM_BASE_URL,
    },
    stdio: 'inherit',
  },
);

function shutdown(signal) {
  server.kill(signal);
  rmSync(dbDir, { recursive: true, force: true });
}
process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));
server.on('exit', (code) => {
  rmSync(dbDir, { recursive: true, force: true });
  process.exit(code ?? 0);
});
