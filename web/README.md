# nodum web UI

The human web UI for nodum (Phase 3). React 19 + TypeScript, built by Vite into
`../nodum/_web/`, which the Python process serves at `/` — one process, one
origin, no CORS.

Eight views ship: **login** (`/login`), **editor** (`/editor`,
`/editor/:nodeId`), **search** (`/search`, also the landing view), **review**
(`/review`), **graph** (`/graph`, `/graph/:rootId`), **assets** (`/assets`),
**admin** (`/admin`), and **history** (`/history/:nodeId`).

## Running it

```bash
make web-install          # once — npm ci in web/
make web-dev              # Vite dev server on http://127.0.0.1:5700
uv run nodum serve        # the API on http://127.0.0.1:8600, in another shell
```

The dev server proxies `/api` and `/healthz` to `127.0.0.1:8600`, so develop
against `http://127.0.0.1:5700` and let the proxy reach the Python process.
The header shows a `connected` / `no server` pill so you can tell at a glance
whether the backend is answering.

For the packaged path:

```bash
make web-build            # tsc --noEmit && vite build -> ../nodum/_web/
uv run nodum serve        # now serves the real bundle at /
make web-clean            # drop the bundle, restore the placeholder
```

`npm run build` type-checks first, so a type error fails the build. CI runs the
same thing in the `Frontend build` job.

## Testing

```bash
make web-test             # or: cd web && npx vitest run
cd web && npx vitest      # watch mode while working
```

[Vitest](https://vitest.dev) over the **pure modules** in `src/` — no component
rendering, no `@testing-library`. A test lives beside the module it covers
(`src/lib/time.test.ts`), and `vitest.config.ts` is kept separate from
`vite.config.ts` so a test setting can never change what ships in the wheel.

The default environment is `node`. One suite opts out —
`views/editor/markdownRender.test.ts` sets `// @vitest-environment jsdom` in its
own docblock, because the preview's sanitising policy *is* a parse and asserting
it against strings would assert against the wrong thing. The opt-out is per
file; the global config stays `node`.

Covered today: `lib/time.ts`, `lib/failure.ts`, `lib/session.ts`,
`views/failureRouting.ts`, `views/graph/filters.ts`,
`views/graph/truncation.ts`, `views/history/unifiedDiff.ts`,
`views/search/signals.ts`, `views/review/grouping.ts`,
`views/admin/grants.ts`, `views/login/credentials.ts`,
`views/editor/markdownRender.ts` + `views/editor/mermaidRender.ts` (the
sanitising policies), and `views/editor/leftoverBuffer.ts`.

**The run pins `TZ` to `Asia/Kathmandu`, and this matters.** SQLite's
`datetime('now')` is UTC with no zone marker, so the bug `lib/time.ts` fixes —
`new Date(s)` reading it as local time — produces *the same instant* on a UTC
machine. Every CI runner is UTC, so a test that trusted the ambient zone would
pass while the code was broken. The pin makes the bug visible; `time.test.ts`
asserts the pin took effect, so dropping it fails the suite rather than quietly
turning it into a tautology. Measured: with the normalisation removed from
`parseTimestamp`, 12 of the 20 timestamp tests fail under the pin and only 4
under UTC.

Kathmandu specifically because it is UTC+05:45 with no DST — a non-integer
offset catches an hours-only assumption as well as a zone-less one, and the
expected instants do not move between summer and winter.

Since the harness is unit-only, anything React renders is still verified by
type-checking it and driving it in a browser.

## Module map

| Path | What lives there |
|---|---|
| `src/main.tsx`, `src/App.tsx`, `src/router.tsx` | entry, app shell (header, nav, toasts, crash boundary, health pill), route table |
| `src/api/client.ts`, `src/api/types.ts` | the only `fetch` in the app, and the types mirroring `nodum/models.py` |
| `src/lib/` | cross-view plain functions: timestamp parsing (`time.ts`) and failure classification (`failure.ts`) |
| `src/components/` | shared React components: `NodeBadge`, `Toast`, `Spinner`, `EmptyState`, `ErrorBoundary` |
| `src/styles/` | `tokens.css`, `base.css`, `primitives.css`, `app.css` |
| `src/views/editor/` | CodeMirror-6 Markdown source editor, slash commands, `[[` autocomplete, live Mermaid preview, autosave |
| `src/views/search/` | query box, ranked hits, per-signal breakdown, signal grouping |
| `src/views/review/` | proposal queue, per-kind cards, proposed-version diffs |
| `src/views/graph/` | Cytoscape subgraph render, filters, path panel |
| `src/views/assets/` | rendition grid, lightbox, uploader, thin JSON export |
| `src/views/login/` | password login against `POST /api/login` |
| `src/views/admin/` | accounts and grants administration, show-once token dialog |
| `src/views/history/` | per-node version timeline and side-by-side diff |
| `package.json`, `vite.config.ts`, `vitest.config.ts`, `tsconfig.json` | toolchain |
| `**/*.test.ts` | Vitest unit tests, beside the module each covers |

Conventions that hold across the tree:

- **A view's entry component keeps a default export.** Routes are lazily loaded,
  which is what keeps CodeMirror, Mermaid, and Cytoscape out of the initial
  bundle; `lazy()` needs the default export.
- **View-local components, hooks, and CSS live in the view's own directory**,
  e.g. `src/views/graph/GraphToolbar.tsx` and `src/views/graph/graph.css`.
  Import the CSS from the component, not from `src/styles/index.css`.
- **Views never import each other.** They link by URL. `router.tsx` is where
  those paths are defined and is the only place they are written down twice —
  grep for the path string before renaming one.
- **Promote to `src/lib/` or `src/components/` when a second view needs it**,
  not before. Both are inherited by every view, so a change there is a change
  everywhere.
- **Timestamps go through `src/lib/time.ts`.** SQLite's `datetime('now')` is UTC
  with no zone marker, so `new Date(row.created_at)` reads it as *local* time
  and prints the wrong clock. `parseTimestamp` normalises it; nothing else may
  call `new Date()` on a server string. (`new Date()` on a client-side epoch
  number — "saved at", "checked at" — is fine.)
- **Failures go through `src/lib/failure.ts`.** `describeFailure` is the one
  place that tells *the API refused this* apart from *nothing was listening*,
  which is not a single test: same-origin an unreachable server is a `fetch`
  `TypeError`, but behind the dev proxy it is a **502** and therefore an
  `ApiError`. Views map its `kind` onto their own panels; they do not re-derive
  it.
- **Logic worth testing lives in a plain module, not in a component.** The
  harness is unit-only, so a rule that matters (a URL codec, a diff parser, a
  grant grid) goes in its own `.ts` with a `.test.ts` beside it, and the
  component consumes it.
- **A dialog locks body scroll and hands focus somewhere real.** `review/Modal`
  and `assets/AssetLightbox` both set `body.style.overflow` on open and restore
  it on close. On close each returns focus to its opener *only if the opener is
  still in the document*; when it is not — the usual case after a confirm that
  emptied the selection — the view places focus instead, because focusing a
  detached node lands the user on `<body>`.

## What is already installed

| Package | For |
|---|---|
| `react`, `react-dom` (19), `react-router-dom` (7) | shell and routing |
| `@codemirror/state`, `@codemirror/view`, `@codemirror/lang-markdown`, `@codemirror/commands`, `@codemirror/autocomplete`, `@codemirror/language`, `@codemirror/theme-one-dark` | editor slice |
| `mermaid` | live diagram preview in the editor |
| `marked` | Markdown preview rendering |
| `dompurify` | sanitising the preview's HTML and mermaid's SVG before either reaches `innerHTML` |
| `cytoscape`, `@types/cytoscape`, `cytoscape-fcose` | graph slice |
| `vite`, `@vitejs/plugin-react`, `typescript`, `@types/react`, `@types/react-dom` | toolchain |
| `vitest` | the unit harness — no component-testing stack |
| `jsdom` | the DOM environment the sanitiser suite alone opts into |

`cytoscape-fcose` ships no types; a minimal declaration lives in
`types/cytoscape-fcose.d.ts`.

## The API client

`src/api/client.ts` is the only place that calls `fetch`. It covers the whole
Phase-3 endpoint surface, typed, and every route it names is served by
`nodum.http_api`.

What it handles for you:

- prefixes `/api` (`getHealth` is the one exception — `/healthz` sits outside);
- unwraps the `{"<plural>": [...], "count": n}` list envelope, so list calls
  return a plain array;
- raises `ApiError` (carrying `status`, `type`, `message`) on any non-2xx, with
  `isNotFound` / `isForbidden` / `isRetryable` helpers;
- takes an optional `AbortSignal` on every call — use it in `useEffect` cleanup.

```ts
import { api, ApiError } from "../../api/client";
import { useToast } from "../../components";

const toast = useToast();
try {
  const nodes = await api.listNodes({ type: "note", state: "active" });
} catch (error) {
  toast.showError(error); // formats ApiError's type and message
}
```

**Never send an identity.** The HTTP surface is the human surface: the server
attributes every write to the session's human principal, and a client-supplied
identity is ignored. The client has no parameter for one, deliberately.

`src/api/types.ts` mirrors `nodum/models.py` field for field. If you change a
pydantic model, change it here in the same commit.

## Styling

Plain CSS, no framework. `src/styles/tokens.css` holds the custom properties;
use them rather than literal values.

Two conventions carry the whole system:

- **The accent (brass) means "you can act on this"** — focus rings, links,
  primary actions, selection. It never encodes anything about the data.
- **The state ramp encodes the service-layer state machine**: `proposed` violet,
  `active` sea-green, `archived` deliberately the lowest-contrast colour in the
  system. Use `<NodeBadge type state />` rather than rolling your own pill.

Class names are prefixed `nd-` because Mermaid and Cytoscape inject global
stylesheets using names like `.node`, `.label`, and `.edge`. Keep the prefix.

Primitives available: `.nd-button` (`--primary`, `--danger`, `--ghost`,
`--small`), `.nd-input` / `.nd-textarea` / `.nd-select` / `.nd-field`,
`.nd-card`, `.nd-badge`, `.nd-empty`, `.nd-spinner`, `.nd-modal`. Utilities:
`.nd-mono`, `.nd-label`, `.nd-meta`, `.nd-truncate`, `.nd-stack`, `.nd-row`,
`.nd-sr-only`. Layout: wrap your view in `.nd-view` (or `.nd-view--wide` if you
manage your own space, as the graph and editor will).

Reduced motion is honoured globally and `:focus-visible` is styled once, for
everything — do not remove an outline anywhere.

## What the server does for the frontend

Both settled; noted here because breaking either is invisible in dev.

1. **The SPA catch-all.** Client routes like `/graph/:rootId` and
   `/editor/:nodeId` are real URLs a user reloads and bookmarks.
   `StaticFiles(html=True)` serves `index.html` for `/` but 404s on those, so
   `create_app` returns `index.html` for any non-`/api`, non-`/healthz` GET
   instead. The Vite dev server does the same thing, so a regression here would
   only show up in the packaged app.
   **`/favicon.ico` is exempted.** A browser asks for it on its own and it is
   not a client route, so the catch-all answering it with `index.html` under a
   200 told the browser it had received an icon. It now gets the bundle's
   `favicon.ico` if one exists and a **204** otherwise — this page declares its
   icon as an inline data URI, so normally there is no file to serve and the
   honest answer is "nothing here".
2. **The port.** `nodum serve` defaults to `127.0.0.1:8600` and the dev proxy in
   `vite.config.ts` targets the same. Change one and change the other.
3. **`nodum/_web/` is gitignored whole.** Vite's `emptyOutDir` wipes it on every
   build, so nothing tracked can live there. When the bundle is absent
   `create_app` serves `nodum/_web_placeholder.html` — a tracked, packaged "UI
   not built" page that sits outside the output directory.
