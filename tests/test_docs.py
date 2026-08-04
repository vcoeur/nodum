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

#: The job `release.yml` publishes from; everything else in that file gates it.
PUBLISH_JOB = "build-and-publish"

#: Jobs of a pull-request-triggered workflow that a tag deliberately does not
#: re-run, each with the reason. Keys are `<workflow>:<job id>` because job ids
#: collide across files (`web` is in two). An entry here is a decision somebody
#: made and wrote down, not a shrug — that is the whole difference between an
#: exemption and the silent gap finding M3 was.
RELEASE_GATE_EXEMPT: dict[str, str] = {
    "docs.yml:deploy": (
        "publishes the Pages site, and is `if: github.event_name != 'pull_request'` "
        "so it never runs as a PR check in the first place. Docs follow main, not "
        "the released version, so a tag must not deploy them."
    ),
}

#: PR job -> the `release.yml` job id that re-runs it, where the two workflows
#: spell the same check differently. Everything else maps by identity.
RELEASE_GATE_ALIASES: dict[str, str] = {
    # docs.yml's job is `build` because that file has only one; in release.yml
    # a job called `build` would read as the wheel build, next to
    # `build-and-publish`.
    "docs.yml:build": "docs",
}

#: `run:` commands a release job may drop from its PR counterpart, with the
#: reason. Keyed by the exact command, so a step that changes shape loses its
#: exemption and has to be re-argued.
RELEASE_STEP_EXEMPT: dict[str, str] = {
    "cd web && npm audit --audit-level=high": (
        "deliberately not a release blocker — the PR leg runs it with "
        "`continue-on-error` for the same reason: an advisory published overnight "
        "must annotate a build, never stop a tag that was green an hour earlier."
    ),
}

#: One mapping key: its name and whatever follows the colon. Wider than
#: GitHub's own job-id rule (`[A-Za-z_][A-Za-z0-9_-]*`) on purpose — the point
#: is that nothing key-shaped slips past unrecognised.
_KEY = re.compile(r" *(?P<key>[A-Za-z0-9_.-]+):(?P<value>.*)$")

#: A `run:` step, in either the `- run: cmd` or the `run: cmd` (named step)
#: shape.
_RUN = re.compile(r" +(?:- )?run: (?P<command>.+)$")


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


def _workflow_lines(name: str) -> list[str]:
    return (WORKFLOWS / name).read_text(encoding="utf-8").splitlines()


def _is_ignorable(line: str) -> bool:
    """Blank lines and comments carry no structure, at any depth."""
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _top_level_key(lines: list[str], key: str, source: str) -> int:
    """The index of the line declaring top-level ``key:``."""
    for index, line in enumerate(lines):
        if _is_ignorable(line) or _indent(line) != 0:
            continue
        match = _KEY.fullmatch(line)
        if match is None or match.group("key") != key:
            continue
        assert not match.group("value").strip(), (
            f"{source} line {index + 1}: `{key}:` is written inline as "
            f"{line.strip()!r}; this parser only reads the block form. Rewrite it "
            "as a block, or teach the parser — an unread `on:`/`jobs:` block is a "
            "workflow this test would not know exists."
        )
        return index
    raise AssertionError(f"{source} has no top-level `{key}:` key")


def _nested_lines(lines: list[str], header_index: int) -> list[str]:
    """Every line nested under the mapping key at ``header_index``."""
    depth = _indent(lines[header_index])
    body: list[str] = []
    for line in lines[header_index + 1 :]:
        if not _is_ignorable(line) and _indent(line) <= depth:
            break
        body.append(line)
    return body


def _child_keys(lines: list[str], header_index: int, source: str) -> dict[str, int]:
    """The immediate child keys of a mapping, as ``{key: line index}``.

    Every line at the children's indent has to parse as a key. That hard
    failure is the point: the previous version of this helper matched
    ``  [a-z0-9-]+:`` and *skipped* anything else, so a job id with an
    underscore (`type_check`), a dot (`test-3.12`) or a capital (`Lint`) was
    invisible — never required by `build-and-publish`, and this test green
    about it. A parse that cannot see a job must fail, never shrug.
    """
    header_indent = _indent(lines[header_index])
    child_indent: int | None = None
    keys: dict[str, int] = {}
    for index, line in enumerate(lines[header_index + 1 :], start=header_index + 1):
        if _is_ignorable(line):
            continue
        indent = _indent(line)
        if indent <= header_indent:
            break
        if child_indent is None:
            child_indent = indent
        if indent > child_indent:
            continue  # nested inside the child currently open
        assert indent == child_indent, (
            f"{source} line {index + 1}: {line.strip()!r} is indented {indent}, between "
            f"the {header_indent} of {lines[header_index].strip()!r} and its children's "
            f"{child_indent}. This hand-parse cannot place it."
        )
        match = _KEY.fullmatch(line)
        assert match is not None, (
            f"{source} line {index + 1}: cannot read {line.strip()!r} as a key under "
            f"{lines[header_index].strip()!r}. This file parses workflows by hand (no "
            "YAML dependency in the dev group for one assertion), so a shape it does "
            "not understand fails here — a job id silently skipped is an ungated "
            "release."
        )
        keys[match.group("key")] = index
    return keys


def _job_ids(name: str) -> set[str]:
    """The job ids one workflow declares."""
    lines = _workflow_lines(name)
    jobs = set(_child_keys(lines, _top_level_key(lines, "jobs", name), name))
    assert jobs, f"{name} declares no jobs — the parser read its `jobs:` block as empty"
    return jobs


def _job_lines(name: str, job_id: str) -> list[str]:
    """The lines of one job's body."""
    lines = _workflow_lines(name)
    jobs = _child_keys(lines, _top_level_key(lines, "jobs", name), name)
    assert job_id in jobs, f"{name} has no `{job_id}` job"
    return _nested_lines(lines, jobs[job_id])


def _job_run_steps(name: str, job_id: str) -> list[str]:
    """The shell commands one job runs, in order.

    `run:` steps only: `uses:` steps name actions, and comparing those across
    two workflows would compare checkout pins rather than checks.
    """
    steps: list[str] = []
    for line in _job_lines(name, job_id):
        if _is_ignorable(line):
            continue
        stripped = line.strip()
        if not (stripped.startswith("run:") or stripped.startswith("- run:")):
            continue
        match = _RUN.fullmatch(line)
        assert match is not None, (
            f"{name}'s `{job_id}` job has a `run:` step this parser cannot read: "
            f"{stripped!r}. A block scalar (`run: |`) or any other shape has to be "
            "taught here rather than skipped — a step this test cannot see is a step "
            "a release may drop unnoticed."
        )
        steps.append(match.group("command").strip())
    return steps


def _triggers_on_pull_request(name: str) -> bool:
    lines = _workflow_lines(name)
    triggers = _child_keys(lines, _top_level_key(lines, "on", name), name)
    return "pull_request" in triggers or "pull_request_target" in triggers


def _pr_gate_workflows() -> list[str]:
    """Every workflow a pull request can trigger.

    Discovered, not listed: the version of this test that named ci.yml and
    release.yml by hand could not see docs.yml, so the strict mkdocs build —
    the check that catches an orphan page or a dead link — had no release
    counterpart and nobody noticed. (docs.yml's trigger carries a `paths:`
    filter, so it gates only PRs touching the docs. A tag cannot know whether
    the docs changed, so release.yml runs it unconditionally.)
    """
    suffixes = {".yml", ".yaml"}
    workflows = sorted(path.name for path in WORKFLOWS.iterdir() if path.suffix in suffixes)
    assert workflows, f"no workflow files under {WORKFLOWS}"
    return [name for name in workflows if _triggers_on_pull_request(name)]


def _publish_needs() -> set[str]:
    """The jobs `build-and-publish` waits for, in either YAML list shape.

    Anchored to the job rather than to the first `needs:` in the file: that
    was correct only by declaration order, and a `needs:` added to any job
    above `build-and-publish` would silently become the list this test checked.
    """
    body = _job_lines("release.yml", PUBLISH_JOB)
    for index, line in enumerate(body):
        if _is_ignorable(line):
            continue
        match = _KEY.fullmatch(line)
        if match is None or match.group("key") != "needs":
            continue
        inline = match.group("value").strip()
        if inline:
            assert inline.startswith("[") and inline.endswith("]"), (
                f"release.yml: `{PUBLISH_JOB}` has a `needs:` this parser cannot read: "
                f"{inline!r}. Use the inline `[a, b]` form or a `- item` list."
            )
            return {item.strip() for item in inline[1:-1].split(",") if item.strip()}
        items = {
            follower.strip()[2:].strip()
            for follower in _nested_lines(body, index)
            if not _is_ignorable(follower) and follower.strip().startswith("- ")
        }
        assert items, (
            f"release.yml: `{PUBLISH_JOB}`'s `needs:` opens a block with no `- item` "
            "lines under it — an empty gate reads as a satisfied one."
        )
        return items
    raise AssertionError(f"release.yml's `{PUBLISH_JOB}` job has no `needs:`")


def test_the_tag_gate_is_not_weaker_than_the_pr_gate() -> None:
    """Every job a pull request runs has a release.yml job, gating the publish
    and running at least the same commands (finding M3).

    Exactly what is asserted, and nothing beyond it: for each workflow carrying
    a `pull_request:` trigger, every job id either maps to a job in release.yml
    (by id, or through RELEASE_GATE_ALIASES) or is named in
    RELEASE_GATE_EXEMPT; that counterpart's `run:` commands are a superset of
    the PR job's, minus the commands named in RELEASE_STEP_EXEMPT; and every
    release.yml job is in `build-and-publish`'s `needs:`. Job names, step
    names, `uses:` steps, matrix legs, `if:` conditions and step order are NOT
    compared — the claim is about which commands run before a wheel is
    published, not about the two files being one file.

    A tag push triggers none of the PR workflows, so release.yml has to re-run
    their checks itself. It did not: ruff, pyright, the highest-resolution leg
    and the whole frontend suite ran on pull requests only (finding M3), and
    the strict docs build did too, while the comment above `needs:` claimed the
    opposite. Asserting it here is what makes that comment checkable — and
    asserting the *steps*, not just the job ids, is what keeps two jobs sharing
    an id from being taken for the same check, which is what `web` was: same
    name, no `tsc --noEmit` on the release side.
    """
    pr_workflows = _pr_gate_workflows()
    assert set(pr_workflows) >= {"ci.yml", "docs.yml"}, (
        f"expected at least ci.yml and docs.yml to be pull-request-triggered, found "
        f"{pr_workflows}. A discovery that quietly finds nothing would make every "
        "assertion below vacuous."
    )

    release_jobs = _job_ids("release.yml")
    assert PUBLISH_JOB in release_jobs, f"release.yml has no `{PUBLISH_JOB}` job"
    needs = _publish_needs()

    missing: list[str] = []
    weaker: list[str] = []
    for workflow in pr_workflows:
        for job in sorted(_job_ids(workflow)):
            qualified = f"{workflow}:{job}"
            if qualified in RELEASE_GATE_EXEMPT:
                continue
            counterpart = RELEASE_GATE_ALIASES.get(qualified, job)
            if counterpart not in release_jobs:
                missing.append(f"{qualified} (expected release.yml:{counterpart})")
                continue
            gated = _job_run_steps("release.yml", counterpart)
            dropped = [
                command
                for command in _job_run_steps(workflow, job)
                if command not in gated and command not in RELEASE_STEP_EXEMPT
            ]
            if dropped:
                weaker.append(f"{qualified} -> release.yml:{counterpart} drops {dropped}")

    assert not missing, (
        f"pull-request jobs with no counterpart in release.yml: {missing}. A tag would "
        "publish without them — add the job to release.yml (and to "
        f"`{PUBLISH_JOB}`'s `needs:`), map it in RELEASE_GATE_ALIASES if it is spelled "
        "differently there, or name it in RELEASE_GATE_EXEMPT with a reason."
    )
    assert not weaker, (
        f"release.yml jobs that run less than their pull-request counterpart: {weaker}. "
        "Same job id is not the same check — add the missing steps, or name each "
        "command in RELEASE_STEP_EXEMPT with the reason a release does not need it."
    )

    unknown = needs - release_jobs
    assert not unknown, (
        f"`{PUBLISH_JOB}` needs jobs release.yml does not declare: {sorted(unknown)}. "
        "A misspelled dependency gates on nothing."
    )
    ungated = release_jobs - needs - {PUBLISH_JOB}
    assert not ungated, (
        f"release.yml jobs that do not gate publishing: {sorted(ungated)}. "
        "A job that runs beside the publish instead of before it stops a release from "
        f"nothing — add it to `{PUBLISH_JOB}`'s `needs:`."
    )
