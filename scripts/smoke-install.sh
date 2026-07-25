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
# The web bundle: `nodum/_web/` is gitignored and produced by `make web-build`,
# so a wheel built from a clean checkout ships the "UI not built" placeholder.
# Set NODUM_SMOKE_REQUIRE_WEB=1 (the release workflow does) to make a missing
# bundle a failure rather than a note — that is what stops a placeholder wheel
# reaching PyPI. Left unset, the script still runs everything else, so a local
# run needs no Node toolchain.
set -euo pipefail

# Run from the repo root regardless of caller CWD (the script lives in scripts/).
cd "$(dirname "${BASH_SOURCE[0]}")/.."

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
dist="$work/dist"
venv="$work/venv"

# Plain `uv build`, NOT `uv build --wheel` — this must exercise the exact build
# path release.yml publishes from. The two differ: `uv build` builds the sdist
# and then builds the wheel *from* it, while `--wheel` reads the source tree
# directly. v0.2.0 shipped a placeholder-UI wheel because the smoke test used
# `--wheel` and so tested a build the release never performs.
echo "==> Build (sdist, then wheel from sdist — as the release does)"
uv build --out-dir "$dist"
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

# Assert against the INSTALLED tree, not the source tree: the question is what
# the wheel actually shipped, which is what a `pipx install` gets.
echo "==> web bundle in the installed wheel"
site_web="$("$venv/bin/python" -c 'import nodum, pathlib; print(pathlib.Path(nodum.__file__).parent / "_web")')"
if [ -f "$site_web/index.html" ] && [ -d "$site_web/assets" ]; then
    echo "    bundle present ($(find "$site_web" -type f | wc -l) files)"
elif [ "${NODUM_SMOKE_REQUIRE_WEB:-}" = "1" ]; then
    echo "FAIL: the wheel has no UI bundle at nodum/_web/ — it would serve the" >&2
    echo "      'UI not built' placeholder. Run 'make web-build' before building." >&2
    exit 1
else
    echo "    no bundle — this wheel serves the placeholder UI."
    echo "    (set NODUM_SMOKE_REQUIRE_WEB=1 to make that fatal)"
fi

echo "OK: clean install of ${wheels[0]##*/} runs and self-describes."
