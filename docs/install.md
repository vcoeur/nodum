---
title: Install · nodum
description: Install nodum from PyPI with pipx or uv, choose the embedding extra, and point it at a database.
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

`serve` refuses a non-loopback bind without `--token` and prints the database
path plus the `#token=…` URL on stderr.

!!! note
    The web UI ships only in wheels whose build ran the frontend build step.
    A wheel built without it serves an "UI not built" placeholder; the CLI,
    the HTTP API, and the MCP server are unaffected.

## Using it from an agent

```sh
nodum mcp serve --actor agent:researcher
```

This speaks MCP over stdio and exposes the read and additive tool tiers only.
Writes from that actor land as `proposed` and wait for a human to accept them.
