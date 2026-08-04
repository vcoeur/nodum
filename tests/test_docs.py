"""Generated documentation is locked: the committed page must be the
generator's output.

``docs/http-api.md`` is generated from the live HTTP route table by
``scripts/gen-http-api-docs.py`` (finding M39) — the page has no
hand-maintained route list to drift, because the generator walks the same
``api_routes`` table the runtime and the session-gate tests read. The
committed page is the lock: this file runs the *real* generator into a
scratch file and compares bytes, so a route added, renamed, re-verbed, or
removed without regenerating the page — or a hand-edit of the page — fails
here instead of shipping as silent drift.

Shelling out to the script rather than re-implementing the walk keeps the
script the single source of truth: the lock exercises the same code path the
regeneration command runs, so the two cannot drift apart.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = REPO_ROOT / "scripts" / "gen-http-api-docs.py"
HTTP_API_DOC = REPO_ROOT / "docs" / "http-api.md"


def test_http_api_doc_matches_the_live_route_table(tmp_path) -> None:
    """docs/http-api.md is exactly what the generator emits from today's app."""
    generated = tmp_path / "http-api.md"
    subprocess.run(
        [sys.executable, str(GENERATOR), str(generated)],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
    )
    expected = generated.read_text(encoding="utf-8")
    actual = HTTP_API_DOC.read_text(encoding="utf-8")
    assert actual == expected, (
        "docs/http-api.md drifted from the live route table — regenerate it with "
        "`uv run python scripts/gen-http-api-docs.py` and commit the result"
    )
