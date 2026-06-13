---
title: Quick start · nodum
description: Run the nodum full app with Docker (or install the CLI from PyPI), sign in, then build a first typed graph — create nodes, link a typed edge, search, and expand a subgraph.
---

# Quick start

nodum ships two ways: the **Docker image** is the full app (API + web UI), and the **PyPI wheel** is
the CLI / library. Pick the track that fits, then build a first graph at the bottom — the graph steps
are the same either way, since the CLI and the API call the same service.

Both tracks need a **PostgreSQL** to point at. The Docker compose example brings its own; the CLI
track expects one you provide (a local container is fine).

## A. Run the full app with Docker

The image bundles the API and the built React UI. Point it at a Postgres and hand it a password
secret; on first boot it waits for the database, creates the schema, sets the main password, and
serves on `0.0.0.0:8600`.

```bash
# 1. grab the turnkey compose file (nodum + Postgres + a password secret)
curl -O https://raw.githubusercontent.com/vcoeur/nodum/main/docker-compose.example.yml

# 2. put the initial main password in the secret file it expects
echo 'change-me' > nodum_admin_password.txt

# 3. up
docker compose -f docker-compose.example.yml up
```

Open <http://127.0.0.1:8600>, and **sign in** with the password from step 2. The image does not
bundle Postgres — `docker-compose.example.yml` adds one for you; in production you point
`NODUM_DATABASE_URL` at your own. Full configuration is in [Install &amp; run](install.md#docker-the-full-app).

## B. Install the CLI from PyPI

The wheel gives you the `nodum` command and the HTTP API — **no web UI** (that ships only in the
image). Good for scripting, automation, or embedding the service.

```bash
pipx install nodum        # or: uv tool install nodum
nodum --help
```

Point it at a Postgres and initialise the schema. The default URL matches a local dev Postgres on
host port 5436:

```bash
export NODUM_DATABASE_URL=postgresql://nodum:nodum@localhost:5436/nodum
nodum init-db                     # create the schema + seed the default kind catalog
nodum auth set-password           # set the main password (gates the API + web; prompts twice)
```

The CLI talks straight to the database through the service layer — it is trusted and never logs in.
The password you set here gates the **network** surfaces (API + web UI); `nodum auth set-password`
is also how you set it for a Docker deployment if you skip the secret.

## Your first graph

These commands work the same whether you run them locally (`nodum …`) or inside the container
(`docker compose exec nodum nodum …`). The web UI does all of this through schema-driven forms — the
CLI is shown here because it copy-pastes.

### Create two typed nodes

`add KIND CONTENT` creates a node; `CONTENT` is the node's plain-text body, and `--set key=value`
carries the kind's typed fields (each value is parsed as JSON, falling back to the raw string):

```bash
nodum add Person "Ada Lovelace" --set born=1815
nodum add Reference "Lovelace, Notes on the Analytical Engine (1843)" \
  --set year=1843 --set 'authors=["Ada Lovelace"]'
```

Each call prints the created node as JSON, including its `uuid` — copy those for the next step.

### Link them with a typed edge

`link FROM TO EDGE_KIND` creates a directed edge. The endpoints are checked against the edge kind's
signature — `AuthorOf` is `Person → Reference`, so the order matters:

```bash
nodum link <ada-uuid> <reference-uuid> AuthorOf
```

### Search

Ranked Postgres full-text search, optionally filtered by kind:

```bash
nodum search "analytical engine" --kind Reference
```

### Expand a subgraph

`expand` walks outward from a seed node along directed edges, up to `--depth` hops — the connected
neighbourhood serialised as one JSON payload:

```bash
nodum expand <ada-uuid> --depth 2 --edge-kind AuthorOf
```

### Read the live contract

One call returns the whole schema — every node kind, edge kind, and signature. This is how an agent
self-orients before its first write:

```bash
nodum schema            # CLI
# or, against a running server:
curl -s http://127.0.0.1:8600/schema
```

### Evolve the schema

Kinds are stored in the database, so you can add, edit, and delete them at runtime — no code change:

```bash
nodum node-kind add Dataset --group entity --content-label name \
  --fields '{"rows": {"type": "int"}}'
nodum edge-kind add DerivedFrom --from Dataset --to Reference
```

## Where to go from here

- **[Concepts](concepts.md)** — why kinds are typed, what each one is for, the `from → to`
  signatures, and the *open process, closed format* principle.
- **[Install &amp; run](install.md)** — both distribution tracks in full, every environment variable,
  authentication, and migrating an older database.
- **[Commands](commands.md)** — every CLI verb and API route, with the JSON contract.

## Troubleshooting

- `command not found: nodum` — `pipx install nodum` did not add its bin dir to your `$PATH`. Run
  `pipx ensurepath` and reopen the shell.
- Protected routes return **503** — no main password is set yet. Run `nodum auth set-password` (or, in
  Docker, provide the admin-password secret). Until one is set, the install is locked.
- API calls return **401** — you are unauthenticated. Sign in via the web UI, or `POST /auth/login`
  and send the returned token as `Authorization: Bearer <token>`. See [Commands](commands.md#authentication).
- `init-db` hangs or errors — the database is not reachable. Check `NODUM_DATABASE_URL` and that your
  Postgres is up (the Docker entrypoint retries for ~60s on boot).
