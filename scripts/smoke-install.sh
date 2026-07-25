#!/usr/bin/env bash
# Clean-install smoke test.
#
# Builds the wheel and installs it into a FRESH virtualenv with NO dev/test
# dependencies and NO lockfile, so runtime dependencies resolve exactly as a
# plain `pip install nodum` would for an end user (latest `typer`, etc.) — not
# the pinned versions the dev lock happens to hold. Then it exercises the
# installed CLI, in particular `nodum schema-dump`, the self-describing
# contract.
#
# This guards the "works in dev, broken on install" class of bug: an undeclared
# dependency that is only present transitively in dev, or a behaviour change in
# the version users actually get. Such a bug fails here instead of reaching a
# release.
#
# Note: this does NOT assert the web bundle is present. `nodum/_web/` is
# gitignored and built by `make web-build`; a wheel built from a clean checkout
# ships the "UI not built" placeholder instead. Making the released wheel carry
# the real bundle is tracked separately.
set -euo pipefail

# Run from the repo root regardless of caller CWD (the script lives in scripts/).
cd "$(dirname "${BASH_SOURCE[0]}")/.."

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
dist="$work/dist"
venv="$work/venv"

echo "==> Build wheel"
uv build --wheel --out-dir "$dist"
wheels=("$dist"/*.whl)
echo "    ${wheels[0]}"

echo "==> Clean install into a fresh venv (deps resolved fresh, no lock, no dev extras)"
uv venv "$venv"
uv pip install --python "$venv/bin/python" "${wheels[0]}"

bin="$venv/bin/nodum"

echo "==> --version"
"$bin" --version

# Deliberately unset NODUM_DB: schema-dump describes the CLI adapter and must
# not need a database. A regression that made it touch one would fail here.
echo "==> schema-dump self-describes (lists its command surface)"
env -u NODUM_DB "$bin" schema-dump > "$work/schema.json"
"$venv/bin/python" - "$work/schema.json" <<'PY'
import json, sys

schema = json.load(open(sys.argv[1]))
names = {c["name"] for c in schema.get("commands", [])}
required = {"node", "edge", "search", "schema", "schema-dump", "projector", "review", "mcp", "asset"}
missing = required - names
assert not missing, (
    f"nodum schema-dump is missing {sorted(missing)} (introspected {len(names)} commands). "
    "A clean install cannot self-describe — likely a dependency/introspection break."
)
print(f"    schema-dump lists {len(names)} commands, incl. {sorted(required)}")
PY

echo "==> --help renders"
"$bin" --help >/dev/null

echo "OK: clean install of ${wheels[0]##*/} runs and self-describes."
