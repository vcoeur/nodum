---
title: Install & run · nodum
description: Install nodum two ways — the Docker image is the full app (API + web UI), the PyPI wheel is the CLI / library. Configuration, authentication, migrating an older database, and running from a source checkout.
---

# Install & run

nodum is **two artifacts from one codebase**:

- the **Docker image** is the **full app** — API + the built React web UI;
- the **PyPI wheel** is the **CLI / library** — the `nodum` command and the HTTP API, with **no web
  UI**.

Both need a **PostgreSQL** to point at (nodum does not bundle one). Pick the track that matches how
you want to run it.

## Docker — the full app

The image is a multi-stage build: a Node stage compiles the React SPA, and the Python stage installs
the package and copies the bundle to `NODUM_WEB_DIST`. The entrypoint waits for Postgres, runs
`init-db`, sets the main password from the admin secret (only if unconfigured), then serves on
`0.0.0.0:8600`. A deploy is therefore: **declare the image, point `NODUM_DATABASE_URL` at a Postgres,
provide a password secret — done.**

### With the example compose file

`docker-compose.example.yml` is a turnkey start — the nodum image, a Postgres, and a password secret:

```bash
curl -O https://raw.githubusercontent.com/vcoeur/nodum/main/docker-compose.example.yml
echo 'change-me' > nodum_admin_password.txt          # the initial main password
docker compose -f docker-compose.example.yml up      # → http://127.0.0.1:8600
```

The example pulls `ghcr.io/vcoeur/nodum:latest`. To build from a checkout instead, comment that
`image:` line and uncomment `build: .` in the compose file.

### In your own deployment

Point the image at your Postgres and supply the password as a secret (preferred) or an env var:

```yaml
services:
  nodum:
    image: ghcr.io/vcoeur/nodum:latest
    environment:
      NODUM_DATABASE_URL: postgresql://user:pass@your-db:5432/nodum
      NODUM_ADMIN_PASSWORD_FILE: /run/secrets/nodum_admin_password
      NODUM_COOKIE_SECURE: "1"        # set when TLS is terminated in front of nodum
    secrets:
      - nodum_admin_password
    ports:
      - "8600:8600"
```

The image does **not** bundle Postgres — bring your own managed or containerised database. The admin
secret only sets the password on first boot; a later `nodum auth set-password` is not clobbered on
restart.

## PyPI — the CLI / library

```bash
pipx install nodum        # or: uv tool install nodum
nodum --help
```

This gives you the `nodum` command and the HTTP API. It ships **no web UI** — the React bundle is
image-only, so `nodum serve` without `NODUM_WEB_DIST` serves the API alone. Use this track for
scripting, automation, or embedding the service.

Point it at a Postgres and initialise:

```bash
export NODUM_DATABASE_URL=postgresql://nodum:nodum@localhost:5436/nodum
nodum init-db                # create the schema + seed the kind lookup tables (idempotent)
nodum auth set-password      # set the main password that gates the API + web
```

Need a local Postgres for development? The repo's `docker-compose.yml` publishes one on host port
`5436` (`docker compose up -d`), which is what the default `NODUM_DATABASE_URL` above expects.

## Configuration

All configuration is environment variables. A local `.env` at the working directory is read if
present — copy `.env.example` to start. The only required value is `NODUM_DATABASE_URL`.

| Variable | Default | What it does |
|---|---|---|
| `NODUM_DATABASE_URL` | `postgresql://nodum:nodum@localhost:5436/nodum` | PostgreSQL connection string. **Required.** |
| `NODUM_API_HOST` | `127.0.0.1` (image: `0.0.0.0`) | API bind address. |
| `NODUM_API_PORT` | `8600` | API port. |
| `NODUM_WEB_DIST` | unset (image: the built bundle) | Path to the built SPA. When set, `serve` mounts the web UI; when unset, the API runs alone. |
| `NODUM_COOKIE_SECURE` | `0` | Set to `1` to mark the session cookie `Secure` (behind a TLS-terminating proxy). |
| `NODUM_ADMIN_PASSWORD_FILE` | unset | File whose contents seed the main password on first boot (`auth ensure-password`). |
| `NODUM_ADMIN_PASSWORD` | unset | Inline alternative to `NODUM_ADMIN_PASSWORD_FILE`. Prefer the file/secret form. |

## Authentication

The network surfaces — the HTTP API and the web UI — are gated by a **single main password**, set
from the CLI on the machine where nodum runs. The local CLI is trusted: it *sets* the secret and
never logs in. Until a password is set the install is **locked** — protected routes return `503`
pointing at the CLI. Multi-user accounts are out of scope; this is one shared password.

```bash
nodum auth set-password      # set/replace it (prompts twice, or reads a piped stdin line)
nodum auth status            # is a password configured? (never prints the hash)
```

How it works:

- **Storage.** A single-row `auth_secret` table holds an **argon2** hash of the password plus a
  random signing key. argon2 runs only at login and at `set-password` — never on the per-request path.
- **Tokens (dual auth).** Login mints a session token signed with the signing key (`itsdangerous`,
  7-day expiry). Browsers carry it in an **HttpOnly, SameSite=Strict cookie** (`Secure` when
  `NODUM_COOKIE_SECURE=1`); API/CLI clients send it as `Authorization: Bearer <token>`. The
  per-request check verifies only the cheap signature — cookie first, then the Bearer header.
- **Rotation.** `set-password` recomputes the hash but **preserves the signing key**, so changing the
  password does not invalidate live sessions.
- **Defence in depth.** Every response carries `Content-Security-Policy: default-src 'self'`,
  `X-Content-Type-Options: nosniff`, and `X-Frame-Options: DENY`.
- **Open routes.** `GET /healthz`, `POST /auth/login`, `POST /auth/logout`, `GET /auth/session`, and
  (when the SPA is mounted) `GET /` + `/assets`. Everything else requires a valid session.
- **Bootstrap.** `auth ensure-password` sets the password from `NODUM_ADMIN_PASSWORD_FILE` /
  `NODUM_ADMIN_PASSWORD` **only when unconfigured** — this is what the Docker entrypoint uses for a
  hands-off first boot.

## Migrating an older database

If you have a pre-typed (MVP) database from before the metamodel, upgrade it in place — idempotently:

```bash
nodum migrate
```

It adds the `kind` columns, seeds the lookup tables, drops the old type-as-node rows (their edges
cascade), backfills kinds (content nodes become `Note`, carrying their old `data.type` into `role`),
then enforces the new constraints. Safe to re-run.

## Development from a source checkout

```bash
git clone https://github.com/vcoeur/nodum.git
cd nodum
make db-up               # start the local Postgres (docker-compose, host port 5436)
make dev-install         # uv sync --all-groups
make init-db             # create the schema + seed kind tables
uv run nodum auth set-password
make test                # pytest (needs the database up)
```

For the web UI (React + Vite, in `frontend/`):

```bash
make frontend-install    # npm ci
make frontend-dev        # Vite dev server on http://127.0.0.1:5700 (proxies the API to 8600)
# …or build it and serve through FastAPI on 8600:
make dev-web
```

Dev ports: the HTTP API serves on `127.0.0.1:8600`, the Vite dev server on `5700` (preview `5701`),
and the local Postgres is published on host port `5436`. Run `make help` for the full target list.
The package version is derived from the git tag (`vX.Y.Z`) at build time and is never committed.
