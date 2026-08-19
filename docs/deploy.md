---
title: Deploy with Docker · nodum
description: Run nodum in a container — what the image must hold, a Compose example with volumes for the database and the model cache, TLS in front, health checks, upgrades, and backups.
---

# Deploy with Docker

nodum is one process and one SQLite file. The container around it exists to
hold that process, and the volumes around the container exist to keep the file
on storage that survives rebuilds — nothing else. This page shows the pattern
with minimal, generic examples; the repository ships no Dockerfile, because
the packaging is deployment taste, not part of the software.

## What the container must hold

- **The published wheel, not the source tree.** `pip install nodum` installs
  from PyPI, and the wheel is what ships the web UI.
- **The extras the deployment wants.** `embeddings` for vector search, and
  `pdf`/`ocr`/`audio` for the extraction handlers — see
  [Install](install.md) for what each adds and what the `ocr` extra needs
  besides itself.
- **A non-root user.** The server reads and writes its own data, and that is
  the whole of its filesystem surface.

## A minimal image

```dockerfile
FROM python:3.12-slim

# The wheel from PyPI, with the extras that make every ingestion handler
# available. The ocr extra needs the tesseract binary; drop the apt-get
# block when you drop the extra.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir 'nodum[embeddings,pdf,ocr,audio]'

# The graph is one SQLite file and the embedding model is a directory of
# files: both are data, so both live on mounted volumes, and both paths are
# fixed here so the mounts are the same everywhere this image runs.
ENV NODUM_DB=/data/nodum.db \
    NODUM_EMBED_CACHE=/models

# Never run as root. uid 10001 is the owner the compose mounts below are
# created for.
RUN useradd --create-home --uid 10001 nodum \
 && mkdir -p /data /models \
 && chown -R nodum:nodum /data /models
USER nodum

EXPOSE 8600

# Each flag is explained under "Putting TLS in front". The hostname is a
# placeholder: replace it here, or override `command:` in compose.
CMD ["nodum", "serve", "--host", "0.0.0.0", "--allow-host", "<hostname>", "--behind-tls"]
```

## Compose example

```yaml
services:
  nodum:
    image: your-registry.example.com/nodum:0.18.1   # pin a release tag
    restart: unless-stopped
    # No host ports: the reverse proxy on this network reaches the server by
    # service name — see "Putting TLS in front".
    volumes:
      - ./data:/data      # the SQLite file IS the system — bind mount it
      - models:/models    # the embedding model cache — survives rebuilds
    environment:
      NODUM_PUBLIC_URL: https://nodum.example.com   # must match the public URL
      NODUM_EMBED_DOWNLOAD: "1"  # fetch the model on first use; a no-op once cached
    healthcheck:
      test: ["CMD", "python", "-c",
             "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8600/healthz')"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
    command:
      - nodum
      - serve
      - --host
      - 0.0.0.0
      - --allow-host
      - nodum.example.com
      - --behind-tls

volumes:
  models:
```

Two mounts, for two different kinds of persistence:

- `./data` holds the SQLite file, and the SQLite file **is** the system —
  nodes, edges, versions, the event log, derived indexes, even binary assets.
  A bind mount keeps it on the host, so a container rebuild touches nothing;
  a named volume would work, but the file is small and worth owning directly.
- `models` is a named volume for the embedding model cache. Unlike the
  database it is re-creatable — `NODUM_EMBED_DOWNLOAD=1` re-fetches a wiped
  cache — but a wiped cache also means the vector signal is gone until the
  next run of the first embedding-needing operation, so the volume exists to
  keep the download. The cache volume must be writable by the image's user;
  the bind mount needs the same on the host side (`mkdir -p data &&
  chown -R 10001:10001 data` once, before the first start).

## Putting TLS in front

The server itself speaks plain HTTP; a TLS-terminating reverse proxy in front
is the deployment shape. Any proxy works — here is the generic Caddy block,
where `nodum` is the compose service name and `8600` its port:

```caddyfile
nodum.example.com {
    reverse_proxy nodum:8600
}
```

Three flags and one environment variable are what make this shape work, and
each exists for a reason:

- **`--host 0.0.0.0`** — bind every interface, so the proxy can reach the
  server. The default binds `127.0.0.1`. A non-loopback bind is allowed:
  password login, not the bind, is the boundary.
- **`--allow-host nodum.example.com`** — the Host-header allowlist, and the
  DNS-rebinding defence. The server answers only the names it was given
  (matching by name, port ignored), and refuses anything else with a
  `400 UntrustedHost` — which is what a rebinding attack's `Host` would be.
  A loopback-bound server already answers every loopback spelling; a server
  behind a proxy answers the name in front of it, and that name has to be
  named here. `--allow-host '*'` disables the check entirely — do not.
- **`--behind-tls`** — marks the session cookie `Secure`, which is what makes
  a browser send it over the TLS connection the proxy terminated. Omitted,
  the flag defaults to the bind: a non-loopback bind counts as proxied, a
  loopback one does not. Behind a proxy the server still speaks plain HTTP
  even on a loopback socket, so a proxied server passes the flag explicitly;
  only an explicit `--no-behind-tls` on a non-loopback bind triggers the
  plain-HTTP warning at startup.
- **`NODUM_PUBLIC_URL`** — the URL clients reach the server on. Capability
  URLs (the single-use asset upload/download grants) are minted against it,
  and its default is `http://127.0.0.1:8600` — the local bind. Behind a
  proxy, an unset variable mints URLs that point at the server's own
  loopback and die on the first click. Set it to the public URL the proxy
  fronts, exactly as clients will type it.

## Health checks

`GET /healthz` answers `{"status": "ok", "version": "<the running version>"}`
on the server's own port, outside the session gate — a proxy can probe it
without credentials, and the version field is what tells you which release a
container actually runs. The compose example wires it as the container
healthcheck.

## First run

The server needs accounts before it is useful, and account bootstrap is the
CLI's: `nodum human passwd`, `nodum agent create`, `nodum grant`. Run them
against the running container with `docker compose exec nodum nodum …`, or
against the mounted database with a one-off container. The full flow is in
[Configuration](configuration.md#accounts).

The embedding model is **not** downloaded at container start. With
`NODUM_EMBED_DOWNLOAD=1` it is fetched on the first operation that needs it —
a vector search, a projector run, a consolidation cycle — and served from the
cache volume afterwards; without the variable, the model is only used if the
cache already holds it, and hybrid search degrades to BM25 in the meantime.

## Upgrading and rolling back

Pin the image tag in compose and upgrade by moving the pin, then
`docker compose pull && docker compose up -d`. The database and the model
cache sit in their volumes, so the swap touches neither. Rolling back is the
same move in the other direction — run the previous tag — with one caveat:
nodum applies pending migrations when it opens the database, and migrations
are one-way. After a newer version has opened the file, the reliable way back
is the backup taken *before* the upgrade, restored over the file — not the
older image over a migrated database.

## Backups

The graph is one file, so a backup is one command:

```sh
nodum backup /data/backup-$(date +%F).db
```

`backup` writes a consistent snapshot via `VACUUM INTO`, folding whatever
committed rows still live in the `-wal` companion file into the copy — a
plain `cp` of the `.db` while a connection is open can strand them. It
refuses to overwrite an existing non-empty destination and reports
`PRAGMA integrity_check` on the result, so a scheduled run can check the
output rather than trust it.

**It takes the settings with it.** Configuration stored through
`nodum config set` lives in `settings.env` beside the database, so `backup`
copies that file to `<dest>.settings.env` at `0600` and names it in the
result's `settings` field (`null` when the graph has none). Without that, a
by-the-book restore would put the graph back and silently revert every setting
the operator ever stored — the LLM API key included.

Restore is the inverse, and it is now **two** files: stop the server, replace
the database file (and its `-wal`/`-shm` companions, which belong to the same
file), put `<dest>.settings.env` back as `settings.env` beside it, start it
again. Copy it with the mode intact (`install -m 600`, or `cp` followed by
`chmod 600`) — it can hold a credential. A restore that skips it is not wrong,
it is a restore to the *defaults* for everything the environment does not set.

A host cron job running
`docker compose exec nodum nodum backup /data/backup-$(date +%F).db`
against the bind-mounted directory is the whole schedule.
