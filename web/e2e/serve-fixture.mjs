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
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const PORT = process.env.NODUM_E2E_PORT ?? '8699';
const PASSWORD = process.env.NODUM_E2E_PASSWORD ?? 'e2e-secret-password';
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

// Two nodes and an edge between them: enough for a reading view with a
// populated edge rail, which is where the contextual actions live.
const alpha = JSON.parse(
  nodum(['node', 'create', '--type', 'concept', '--title', 'Alpha node', '--as', 'owner']),
);
const beta = JSON.parse(
  nodum(['node', 'create', '--type', 'concept', '--title', 'Beta node', '--as', 'owner']),
);
nodum(['edge', 'create', alpha.id, beta.id, '--type', 'relates_to', '--as', 'owner']);

// Specs read these rather than hardcoding ids the seed might renumber.
process.stdout.write(`${JSON.stringify({ alpha: alpha.id, beta: beta.id, dbPath })}\n`);

const server = spawn('uv', ['run', 'nodum', 'serve', '--db', dbPath, '--port', PORT], {
  cwd: REPO_ROOT,
  stdio: 'inherit',
});

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
