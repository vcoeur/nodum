---
title: Configuration · nodum
description: The settings file, the environment variables, the serve flags, and the account bootstrap — human passwords, agent tokens, and the grants that make an agent usable.
---

# Configuration

nodum is configured by **`settings.env`, a file beside the database**, by
**environment variables**, and by the flags of `nodum serve`. There is no
settings table in the database. The posture is that nothing is on by surprise:
no feature that downloads, spends, or writes unattended is enabled without an
explicit value, and an unset value means the feature is off — not "on with
defaults".

## Where a value comes from

Three layers, and the one furthest right wins:

```text
built-in default  <  settings.env  <  environment variable
```

**Empty is not set, at any layer.** A variable exported as the empty string —
which is exactly what `${VAR:-}` in a compose file renders when the host `.env`
says nothing — is *unset*, not "set to nothing", so it does not shadow a stored
value. Whitespace counts as empty too.

`nodum config list` answers "what is in force and where did it come from" for
every name, per key: `environment`, `settings.env`, `default`, or `unset`.

### The settings file

`settings.env` lives in the database's own directory (`nodum.db` →
`settings.env` beside it), so a graph and its configuration move, are backed up
and are restored together — `nodum backup <dest>` copies it to
`<dest>.settings.env`. It is created `0600` and can hold the API key, so it is
readable by its owner and nobody else.

Its dialect is deliberately small, because it is also what an operator edits by
hand:

- one `KEY=value` per line, LF endings;
- `#` comment lines and blank lines are kept exactly as written;
- **no quoting and no escaping** — a value is what is written after the `=`,
  stripped of surrounding whitespace;
- a value containing a newline or any other control character is **refused at
  the write path**, because written verbatim it would be a second `KEY=value`
  line chosen by whoever supplied the value;
- keys nodum does not recognise are **kept and reported**, never dropped: they
  may belong to a newer version, or to the operator.

Every value is **validated before it is written**, so the file never holds
something the runtime would read back and discard. A file nodum cannot parse is
reported loudly on startup and stepped around — resolution continues on the
environment and the defaults, and `nodum config list` reports `unreadable` with
the reason — and a write against it is refused rather than rewriting a file
whose other lines cannot be preserved. Editing the file by hand is supported and
writes no event; `nodum config set` writes one.

### What cannot be stored there

Four names resolve from the environment and the default alone, and
`nodum config set` refuses them with the reason:

| Variable | Why |
|---|---|
| `NODUM_DB` | Read before the graph — and therefore before its settings file — is open. |
| `NODUM_LLM_BASE_URL` | The endpoint an API key may travel to is a deployment decision. |
| `NODUM_EMBED_CACHE` | A path on the server's own disk. |
| `NODUM_PUBLIC_URL` | Every capability URL is minted from it, so a stored value would redirect them. |

`NODUM_EMBED_MODEL` and `NODUM_EMBED_DOWNLOAD` are not on the settings surface
yet either: changing the model invalidates every stored vector, and resolving a
new one downloads and loads it. They stay environment variables for now.

A name the environment currently sets is also refused — a stored value would
never be used, and an accepted-but-inert edit is the failure this whole surface
exists to avoid. Unset the variable first.

## Environment variables

### Core

| Variable | Default | What it does |
|---|---|---|
| `NODUM_DB` | `~/.local/share/nodum/nodum.db` | The graph database path. Resolution is `--db` flag → this variable → the default. |
| `NODUM_PUBLIC_URL` | `http://127.0.0.1:8600` | The base URL minted capability URLs (single-use asset upload/download grants) are built on. Behind a reverse proxy it must be the public URL, or minted URLs point at the server's own loopback and die — see [Deploy with Docker](deploy.md#putting-tls-in-front). |
| `NODUM_CONSOLIDATE_AT` | unset — off | The nightly consolidation cycle's local wall-clock time, as `HH:MM` (24-hour). `nodum serve` runs the cycle in the process it is already running — no cron, no second process. Unset means off, which is the default; a value that cannot be parsed is announced on stderr and ignored. |

### Embeddings

| Variable | Default | What it does |
|---|---|---|
| `NODUM_EMBED_MODEL` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | The local embedding model (~0.22 GB, multilingual, 384-dimensional). The `embeddings` extra must be installed. |
| `NODUM_EMBED_DOWNLOAD` | unset — never download | `1` allows the one-time model download; without it the model resolves only when it is already in the cache. A no-op once the cache holds the model. |
| `NODUM_EMBED_CACHE` | `~/.local/share/nodum/models` | Where model files are cached. Passed to fastembed explicitly, because fastembed's own default is a temp directory that a reboot can clear — and a cleared cache means the vector signal drops out of search and consolidation with no error anywhere. |

The model is fetched lazily, on the first operation that needs it — a vector
search, a projector run, a consolidation cycle — never at process start, and
never implicitly. A deployment that runs without the model degrades rather
than failing: hybrid search falls back to BM25, and
`nodum projector status` reports the vector index as unavailable. Re-fetch a
wiped cache by rerunning with `NODUM_EMBED_DOWNLOAD=1`.

### Audio transcription

| Variable | Default | What it does |
|---|---|---|
| `NODUM_AUDIO_MODEL` | `base` | The Whisper model size used for audio transcription. |
| `NODUM_AUDIO_DOWNLOAD` | unset — never download | `1` allows the one-time transcription-model download. Transcription models are never fetched implicitly: without it, faster-whisper is held to its local cache, and an uncached model surfaces as an unavailable handler rather than a download. |

### The LLM block

| Variable | Default | What it does |
|---|---|---|
| `NODUM_LLM_MODEL` | unset — no provider | The model name. Unset means no provider, and therefore no smart features anywhere — there is no default, because a guessed model name is a 404 on the first call rather than an honest absence. |
| `NODUM_LLM_BASE_URL` | `http://localhost:11434/v1` | OpenAI-compatible base URL. A spelling that is not a `http(s)` URL — scheme included — is refused, because choosing the scheme on the operator's behalf decides whether the API key crosses the network in clear text. |
| `NODUM_LLM_API_KEY` | unset | Bearer token. Optional (the local default needs none), and sent only to an endpoint somebody named — `NODUM_LLM_BASE_URL`, or a model id a shipped profile serves. |
| `NODUM_LLM_THINKING` | `high` | The reasoning level, one of `none`, `low`, `medium`, `high`. A value outside the set is refused with the list rather than passed on. |
| `NODUM_LLM_CONTEXT_TOKENS` | `4096` | The window the endpoint will actually serve — not the model card's number. Raising it above the *serving* window silently truncates prompts; raise it only together with the serving window. |
| `NODUM_LLM_MAX_OUTPUT_TOKENS` | `4096` | The per-call output ceiling. A call that comes back at it is treated as failed — the body is cut mid-token — so this is sized for the longest legitimate answer, not the average. |
| `NODUM_LLM_CALL_TIMEOUT` | `120` (seconds) | The per-call wall-clock ceiling handed to the provider. |
| `NODUM_LLM_REQUEST_BUDGET` | `8000` | The token budget one human-initiated request (`ask`, `summarize`, `search --nl`) may spend. Unlike the cycle budget this defaults to *on*: a human pressing a button is not an unattended background process. |
| `NODUM_LLM_REQUEST_SECONDS` | `180` (seconds) | The wall-clock ceiling for one human-initiated request. |
| `NODUM_LLM_CYCLE_BUDGET` | `0` | The token budget one consolidation cycle's LLM jobs may spend. **Unset or 0 means those jobs do not run** — which is the default. Fund the abstraction job by setting a budget; `NODUM_LLM_CYCLE_SECONDS` bounds the same work in wall-clock time. |
| `NODUM_LLM_CYCLE_SECONDS` | `1800` (seconds) | The per-cycle wall-clock ceiling, independent of the token budget. |

### What a bad value does

Ten of the seventeen names are checked, and `nodum config set` refuses a bad
value outright — so only a hand-edited `settings.env` or an environment
variable can carry one past the door. What happens then differs by key, on
purpose, and `nodum config list` reports which rule applies as `on_invalid`:

- **`fall-back`** — the value is ignored and the default applies. Every budget
  and ceiling (`NODUM_LLM_CYCLE_BUDGET`, `…_CYCLE_SECONDS`, `…_REQUEST_BUDGET`,
  `…_REQUEST_SECONDS`, `…_CALL_TIMEOUT`, `…_MAX_OUTPUT_TOKENS`) and the download
  gate `NODUM_AUDIO_DOWNLOAD`, whose default is off. The fallback is a smaller
  ceiling, so the worst case is less work.
- **`refuse`** — there is no provider at all, and `nodum llm status` says why.
  `NODUM_LLM_THINKING` and `NODUM_LLM_CONTEXT_TOKENS`. A reasoning level the API
  does not know is not a slower call: it is one the server may accept and not
  honour, spending tokens under a setting nobody chose.
- **`off`** — announced on stderr and ignored, and the feature stays off.
  `NODUM_CONSOLIDATE_AT`; a server that will not boot over a stray character in
  an optional schedule is worse than one that says what it skipped.
- **`null`** — nothing checks it, here or at run time, because there is no
  such thing as an invalid value for it: any non-empty string is accepted.
  The two model names (`NODUM_LLM_MODEL`, `NODUM_AUDIO_MODEL`), the key
  (`NODUM_LLM_API_KEY`) and the four paths and URLs. A model name nothing
  serves is not refused anywhere — it is an HTTP error on the first call, which
  `nodum llm status` is the way to find before you make one.

## Reading and writing settings

```sh
nodum config list                                        # every key, value, provenance
nodum config get NODUM_LLM_MODEL
nodum config set NODUM_LLM_CYCLE_BUDGET 20000 --as human:owner
nodum config unset NODUM_LLM_CYCLE_BUDGET --as human:owner
```

`set` and `unset` name their human like every other write and are recorded in
the event log as `settings.set` / `settings.unset`, with before and after. They
are **not** reversible by `undo`: a file outside the database is not something a
transaction can put back.

A change applies without a restart. The three things that means in practice:

- a **budget** funds the *next* run — an `AgentRun` spans a whole consolidation
  cycle, so lowering the cycle budget does not stop a cycle already spending
  (`nodum cycle-stop <id>` is the kill switch);
- a **model, key, window or reasoning level** applies at the next provider
  resolution, which is the next call;
- the **nightly schedule** applies within a minute — the scheduler re-reads it
  as it sleeps, so turning it on, off, or to another hour needs no restart.

Secrets are never printed. `nodum config get NODUM_LLM_API_KEY` reports
`"set": true` and no value; the event payload records `set`/`unset` rather than
what changed.

`nodum llm status` shows what the provider actually resolves to — the model,
the endpoint, whether a configured key is withheld, the window and the
ceiling — which is the first diagnostic when a smart feature answers `false`
instead of failing. The per-request semantics and the output-envelope shapes
are in [Commands](commands.md#asking-the-graph); wiring a provider and a
cycle budget for the internal agent is walked through in
[The gardener](gardener.md#giving-it-a-model).

## Serve flags

| Flag | Default | What it does |
|---|---|---|
| `--host` | `127.0.0.1` | Interface to bind. A non-loopback bind is allowed: password login, not the bind, is the boundary. |
| `--port` | `8600` | TCP port. |
| `--allow-host` | — | Host names this server answers to (repeatable). The Host-header allowlist is the DNS-rebinding defence: any other Host is refused with `400 UntrustedHost`. Matching is by name, port ignored. A loopback bind already answers every loopback spelling; a name in front of a non-loopback bind has to be named here. `*` disables the check entirely. |
| `--db` | — | Database path for this server, overriding `NODUM_DB` for the process. |
| `--behind-tls` / `--no-behind-tls` | decided by the bind | Whether a TLS proxy fronts this server; decides the `Secure` flag on the session cookie. Omitted, a non-loopback bind counts as proxied and a loopback bind does not — so a server behind a TLS proxy passes it explicitly even on a loopback socket (where the server still speaks plain HTTP), and only an explicit `--no-behind-tls` on a non-loopback bind draws the plain-HTTP warning at startup. |

## Accounts

### Human accounts

Migration seeds one human, `owner`, passwordless. Set its password first —
every later command names its human with the required `--as` flag:

```sh
nodum human passwd --as human:owner     # prompted, never echoed, argon2id at rest
```

`human passwd` takes the account id as its argument (default `owner`) and
prompts for the password — it is never echoed, so it stays out of shell
history and process listings. Passwords are hashed with argon2id and the hash
never enters a command's JSON output.

`nodum human create <name>` adds accounts, `list`/`disable`/`enable` manage
them. Disabling a human kills its sessions **and its agents' tokens** in the
same move — proposals already filed stay reviewable.

### Agent accounts

Agents do not log in. Each agent account owns one bearer token, minted by the
CLI:

```sh
nodum agent create researcher --as human:owner
```

The token prints to stderr **once** (`ndm_…`, ~256 bits) and only its hash is
stored — the plaintext does not exist anywhere after that moment, so the
printed value is the only copy. **The token is not an environment variable**:
there is no `NODUM_AGENT_TOKEN` or equivalent server variable to set. The
token lives where the agent does — in the MCP client config's `headers`
block, per the snippet in [Install](install.md#using-it-from-an-agent).

`nodum agent list` names the accounts; `nodum agent token-rotate <id>` replaces
a token (the old one dies at once, the new one shows once, same rules);
`disable`/`enable` stop and resume one — the principal is re-read from the
database on every request, so a disabled account or a rotated token stops
working at the next call, not at the next restart.

### Grants — the load-bearing step

A token authenticates, but a grant is what lets it see or write anything:

```sh
nodum grant researcher main edit --as human:owner
```

Three levels, hierarchical — `read ⊂ suggest ⊂ edit`:

- `read` — read-only on the space.
- `suggest` — writes land `proposed` and wait in the review queue for a
  human (`read` is included).
- `edit` — writes land `active` immediately (`suggest` and `read` are
  included).

The grant is the load-bearing step, and its absence is silent: **an agent
with no grant on a space cannot even read it**, and an ungranted space
answers identically to a nonexistent one — so the diagnostic of a missing
grant is empty results, not an error. A `read` grant on a space does not
confer anything on the rest of the graph; every space an agent is to use has
to be granted by name.

`nodum grant` re-levels an existing grant (the same command again, a new
level), `nodum revoke <agent> <space>` removes one, and
`nodum grants [--agent <id>]` lists the rows. Grants are event-logged, so
`nodum events` shows who granted what and when. A grant is a ceiling, not a
mandate: an agent holding `edit` may still file `proposed` when it is unsure,
and a request to land above the grant is refused rather than quietly
downgraded.

The default space is `main` — the space `nodum grant … main …` names in every
example here and in [Quick start](quick-start.md). Reviewing what a
`suggest`-granted agent files is the human CLI's job:
`nodum review queue --as human:owner`, then `nodum accept <id>` /
`nodum reject <id> --reason "…"`.
