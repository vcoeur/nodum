#!/bin/sh
# Container bootstrap: wait for Postgres, create/upgrade the schema, set the main
# password from the admin secret (only if unconfigured), then serve. A deploy
# therefore needs just NODUM_DATABASE_URL and an admin-password secret.
set -e

# Wait for the database, then create/seed the schema (both idempotent).
tries=0
until nodum init-db >/dev/null 2>&1; do
  tries=$((tries + 1))
  if [ "$tries" -ge 30 ]; then
    echo "nodum: database not reachable after 30 attempts; final attempt:" >&2
    nodum init-db   # run once more without suppression to surface the error
    exit 1
  fi
  echo "nodum: waiting for database… (attempt $tries)" >&2
  sleep 2
done

# Bootstrap the main password from the secret on first boot (no-op if already set).
nodum auth ensure-password

exec nodum serve --host "${NODUM_API_HOST:-0.0.0.0}" --port "${NODUM_API_PORT:-8600}"
