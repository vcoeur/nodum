---
title: Install · nodum
description: Install nodum from PyPI with pipx or uv, choose the embedding and extraction extras, and point it at a database.
---

# Install

nodum needs **Python ≥ 3.12**. It is published to PyPI on every `vX.Y.Z` tag.

## As a tool (recommended)

[pipx](https://pipx.pypa.io/) keeps the CLI isolated from your other Python
environments:

```sh
pipx install nodum
nodum --version
```

To upgrade later:

```sh
pipx upgrade nodum
```

## As a library

```sh
uv add nodum          # or: pip install nodum
```

## The embedding extra

Vector search uses a local in-process embedding model. It is optional, and
pulls in a much larger dependency tree:

```sh
pipx install 'nodum[embeddings]'
```

Without it nodum runs fine — hybrid search degrades to BM25 keyword ranking,
and `nodum projector status` reports the vector index as unavailable rather
than failing.

## The extraction extras

Ingestion reads plain text, Markdown, JSON, and HTML with the standard
library, so a bare install already ingests. The heavier formats are optional:

| Extra | Adds | Also needs |
|---|---|---|
| `pdf` | PDF text extraction and `page:<n>` page rasters | — |
| `ocr` | text out of images | the `tesseract` binary on your PATH |
| `audio` | speech-to-text for audio files | — |

```sh
pipx install 'nodum[pdf]'
nodum ingest handlers      # what this install can actually read
```

A missing handler is never fatal: the asset is still registered and still
described, and the result says plainly that no text came out. `ingest handlers`
names the extra to install for anything reporting `available: false`.
Transcription models are never downloaded implicitly — like the embedding
model, they are confined to the local cache unless `NODUM_AUDIO_DOWNLOAD=1`
says otherwise.

## Choosing the database

Path resolution, in precedence order:

1. the `--db` flag,
2. the `NODUM_DB` environment variable,
3. `~/.local/share/nodum/nodum.db`.

Create it once:

```sh
nodum init
```

`init` is idempotent — running it against an existing database applies any
pending migrations and reports them.

## Verifying the install

Every command prints one JSON object on stdout, so the install can check
itself:

```sh
nodum --version
nodum schema-dump      # the whole command surface, as JSON
```

`schema-dump` needs no database. If it enumerates the command tree, the
install resolved its dependencies correctly — this is exactly what the
project's clean-install smoke test asserts before any release is published.

## Running the web UI

```sh
nodum serve                       # http://127.0.0.1:8600
```

`serve` prints the database path on stderr and gates every `/api` route on a
password-login session (`nodum human passwd` sets a password first); a
non-loopback bind is allowed — login, not the bind, is the boundary.

!!! note
    The web UI ships only in wheels whose build ran the frontend build step.
    A wheel built without it serves an "UI not built" placeholder; the CLI,
    the HTTP API, and the MCP server are unaffected.

## Using it from an agent

Create an agent account first:

```sh
nodum agent create researcher --as owner    # prints the token once — store it
```

Then point your MCP client at the running server. **There is no subprocess to
launch and no stdio transport**: the agent surface is a route on `nodum serve`,
so the client needs a URL and a header, and the same server can be on this
machine or on another one.

```json
{
  "mcpServers": {
    "nodum": {
      "type": "http",
      "url": "http://127.0.0.1:8600/mcp",
      "headers": {
        "Authorization": "Bearer ndm_…"
      }
    }
  }
}
```

Swap the host for your own to reach a deployed instance —
`https://nodum.example.com/mcp` — and nothing else about the config changes.
The database is the server's business, so there is no `NODUM_DB` here: the
client no longer starts a process and has nothing to point at a file.

`nodum serve` has to be running for any of this to resolve. That is the one
real cost of dropping stdio, where the client started the server on demand.

To check a token before wiring it up, ask the server for its tool list — a
token that verifies no enabled agent gets a `401`, and a good one gets the
read and additive tiers:

```sh
curl -sS -X POST http://127.0.0.1:8600/mcp \
  -H 'Authorization: Bearer ndm_…' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

It exposes the read and additive tool tiers only, and confines every call to
that agent's grants — re-read from the database on every call, so
`nodum agent disable` takes effect at the next one. A `suggest` grant means its
writes land `proposed` and wait for a human.

One token is one agent account is one grant set, so two clients can hold two
tokens with different reach. Ingestion is **by URL or upload**: no tool on this
surface takes a path on the server's filesystem, which matters most when the
server is not the machine the agent runs on.
