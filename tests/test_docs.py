"""Generated documentation is locked, and the release gate's own claim is too.

Two locks live here, both of the same shape: a committed artefact that asserts
something about another file, pinned so the claim cannot outlive it.

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

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = REPO_ROOT / "scripts" / "gen-http-api-docs.py"
HTTP_API_DOC = REPO_ROOT / "docs" / "http-api.md"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

#: Jobs that exist in ci.yml but have nothing to gate on a tag. Empty, and it
#: has to stay a named exemption rather than a shrug: a job listed here is one
#: somebody decided a release does not need.
RELEASE_GATE_EXEMPT: frozenset[str] = frozenset()


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


def _workflow_jobs(name: str) -> set[str]:
    """The job ids one workflow declares, read without a YAML dependency.

    A job id is the only thing indented exactly two spaces under ``jobs:`` —
    every key inside a job is deeper — so the shape is unambiguous without
    pulling PyYAML into the dev group for one assertion.
    """
    lines = (WORKFLOWS / name).read_text(encoding="utf-8").splitlines()
    start = lines.index("jobs:")
    jobs: set[str] = set()
    for line in lines[start + 1 :]:
        if line.strip() and not line.startswith(" "):
            break  # a new top-level key: `jobs:` is over
        if re.fullmatch(r"  [a-z0-9-]+:", line):
            jobs.add(line.strip().rstrip(":"))
    return jobs


def _publish_needs() -> set[str]:
    """What `build-and-publish` in release.yml gates on."""
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    match = re.search(r"^    needs: \[(.+)\]$", text, re.MULTILINE)
    assert match is not None, "release.yml's build-and-publish job has no `needs:` list"
    return {name.strip() for name in match.group(1).split(",")}


def test_the_tag_gate_is_not_weaker_than_the_pr_gate() -> None:
    """Publishing gates on every check a pull request has to pass (finding M3).

    A tag push does not trigger ci.yml, so release.yml has to re-run its
    checks — and for four of them it did not, while the comment above
    `needs:` said the opposite. ruff, pyright, the highest-resolution
    resolution leg and the whole frontend suite ran on pull requests only, so
    the artifact users install was gated more weakly than the branch it came
    from. Asserting it here is what makes the comment checkable: the claim and
    the list cannot drift, and a job added to ci.yml fails this test until
    somebody decides whether a release needs it.
    """
    pr_jobs = _workflow_jobs("ci.yml")
    release_jobs = _workflow_jobs("release.yml")
    needs = _publish_needs()

    missing_from_release = pr_jobs - release_jobs - RELEASE_GATE_EXEMPT
    assert not missing_from_release, (
        f"ci.yml jobs with no counterpart in release.yml: {sorted(missing_from_release)}. "
        "A tag would publish without them — add the job, or name it in RELEASE_GATE_EXEMPT "
        "with a reason."
    )
    ungated = release_jobs - needs - {"build-and-publish"}
    assert not ungated, (
        f"release.yml jobs that do not gate publishing: {sorted(ungated)}. "
        "A job that runs beside the publish instead of before it stops a release from "
        "nothing — add it to build-and-publish's `needs:`."
    )
