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

The second lock, ``test_the_tag_gate_is_not_weaker_than_the_pr_gate``, reads
the workflow files with PyYAML — the same parse GitHub Actions makes of them.
It did not always. It hand-parsed them with regexes, to keep a YAML dependency
out of the dev group for one assertion, and that parser was rewritten twice and
caught mis-reading a workflow *silently* three reviews running:

* ``run: |`` matched the one-line ``run:`` pattern and was recorded as the
  command ``"|"``, so two different block scalars compared equal;
* then nine more — flow-mapping steps dropped, ``uses:``-only jobs read as
  empty, anchors compared as text, quoted scalars truncated, ``run: |2``, a
  ``run:`` nested under ``with:`` counted as a command, duplicate job ids, two
  top-level ``jobs:`` keys;
* then four more — a multi-line plain ``run:`` scalar truncated to its first
  line, so ``ruff check .`` and ``ruff check --exit-zero .`` both read as
  ``ruff check`` and compared equal; a quoted ``"run":`` key and a ``run :``
  with a space before the colon each seen as no step at all; and duplicate
  ``steps:``/``run:`` keys resolved the opposite way round to YAML.

Every one of those read green. That is the argument for the dependency: in the
one test whose whole purpose is catching silent drift, a parser that silently
mis-reads is not a dependency saved, it is the very failure the test exists to
prevent, wearing the test's own clothes. ``pyyaml`` is a test-only entry in the
``dev`` group — it is not in ``[project].dependencies`` and does not ship in
the wheel.

A real parse retires most of the old refusals; the ones below are the ones it
does not. A job with no ``steps:`` list, a ``run:`` that is not a string, a
``needs:`` that is not job ids, a duplicate mapping key: each fails naming the
file, because a workflow read wrong is worse than one not read at all — it
reads green.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = REPO_ROOT / "scripts" / "gen-http-api-docs.py"
HTTP_API_DOC = REPO_ROOT / "docs" / "http-api.md"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

#: The job `release.yml` publishes from; everything else in that file gates it.
PUBLISH_JOB = "build-and-publish"

#: The triggers that make a workflow part of the pull-request gate.
PR_TRIGGERS = frozenset({"pull_request", "pull_request_target"})

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


class _StrictLoader(yaml.SafeLoader):
    """``yaml.SafeLoader`` without its tolerance for duplicate mapping keys.

    PyYAML keeps the last of two same-named keys and says nothing. That is a
    defensible reading — it is the spec's, and GitHub's — but it is not one
    this comparison should take in silence: a second `steps:` or a second
    `run:` slipped into a release job means the job runs something other than
    what a reader of the file above it would say it runs. Refusing names the
    file and the line instead.
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        """Construct a mapping, raising on a repeated key rather than keeping the last."""
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                repeated = key in seen
            except TypeError:
                continue  # an unhashable key; PyYAML raises on it in super() below
            if repeated:
                raise yaml.constructor.ConstructorError(
                    None,
                    None,
                    f"duplicate key {key!r} — one of the two is being silently discarded",
                    key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _workflow(name: str) -> dict[str, Any]:
    """One workflow file, parsed."""
    try:
        document = yaml.load((WORKFLOWS / name).read_text(encoding="utf-8"), Loader=_StrictLoader)
    except yaml.YAMLError as error:
        raise AssertionError(
            f"{name} is not YAML this test can read: {error}. A workflow that does not "
            "parse is a workflow whose checks cannot be compared."
        ) from error
    assert isinstance(document, dict), (
        f"{name} does not parse as a mapping (got {type(document).__name__}); "
        "a workflow with no top-level keys has no jobs to compare."
    )
    return document


def _triggers(name: str) -> set[str]:
    """The event names one workflow declares under `on:`.

    PyYAML implements YAML 1.1, where a bare ``on`` is the boolean *true* — so
    a workflow's trigger block arrives under the key ``True``, not ``"on"``.
    Both spellings are read; declaring the two of them is refused, because
    which one GitHub honours is not a question to answer quietly.
    """
    document = _workflow(name)
    present = [key for key in (True, "on") if key in document]
    assert present, f"{name} has no top-level `on:` key — nothing says when it runs."
    assert len(present) == 1, (
        f'{name} declares both `on:` and a quoted `"on":`. YAML 1.1 reads the first as '
        "the boolean true, so these are two different keys holding two trigger blocks."
    )
    triggers = document[present[0]]
    if isinstance(triggers, str):
        return {triggers}
    if isinstance(triggers, list):
        assert all(isinstance(event, str) for event in triggers), (
            f"{name}: `on:` is a list holding something that is not an event name: {triggers!r}"
        )
        return set(triggers)
    if isinstance(triggers, dict):
        assert all(isinstance(event, str) for event in triggers), (
            f"{name}: `on:` maps something that is not an event name: {sorted(map(repr, triggers))}"
        )
        return set(triggers)
    raise AssertionError(
        f"{name}: `on:` is {triggers!r}, which is neither an event name, a list of them, "
        "nor a mapping of them. A trigger block this test cannot read is a workflow it "
        "cannot tell is part of the pull-request gate."
    )


def _jobs(name: str) -> dict[str, dict[str, Any]]:
    """The jobs one workflow declares, as ``{job id: job}``."""
    jobs = _workflow(name).get("jobs")
    assert isinstance(jobs, dict) and jobs, (
        f"{name} has no non-empty `jobs:` mapping (got {jobs!r}). A workflow whose jobs "
        "cannot be listed is one whose checks this test would silently skip."
    )
    for job_id, job in jobs.items():
        assert isinstance(job_id, str), f"{name}: job id {job_id!r} is not a name."
        assert isinstance(job, dict), (
            f"{name}: job `{job_id}` is {job!r}, not a mapping — it has no steps to compare."
        )
    return jobs


def _job_run_steps(name: str, job_id: str) -> list[str]:
    """The shell commands one job runs, in order.

    ``run:`` steps only: ``uses:`` steps name actions, and comparing those
    across two workflows would compare checkout pins rather than checks. Each
    command is the parsed scalar, stripped of leading and trailing whitespace —
    so ``run: cmd`` and a one-line ``run: |`` block of the same command are the
    same command, and a block scalar's trailing newline is not a difference.
    Everything else about the text is compared exactly.

    A job with no ``steps:`` list fails here rather than reading as a job that
    runs nothing: a job calling a reusable workflow with ``uses:`` is a list of
    checks that would otherwise be invisible to the comparison below. So does a
    ``run:`` that is not a non-empty string.
    """
    jobs = _jobs(name)
    assert job_id in jobs, f"{name} has no `{job_id}` job"
    steps = jobs[job_id].get("steps")
    assert isinstance(steps, list) and steps, (
        f"{name}'s `{job_id}` job has no non-empty `steps:` list (got {steps!r}). A job "
        "that calls a reusable workflow with `uses:` runs checks this comparison cannot "
        "read; teach this test rather than let them go uncompared."
    )
    commands: list[str] = []
    for position, step in enumerate(steps, start=1):
        assert isinstance(step, dict), (
            f"{name} `{job_id}` step {position} is {step!r}, not a mapping — a step whose "
            "keys cannot be read may be a `run:` this comparison would miss."
        )
        if "run" not in step:
            continue
        command = step["run"]
        assert isinstance(command, str) and command.strip(), (
            f"{name} `{job_id}` step {position}: `run:` is {command!r}, not a command. An "
            "empty or non-string `run:` is either a mistake or a shape this test does not read."
        )
        commands.append(command.strip())
    return commands


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
    return [name for name in workflows if _triggers(name) & PR_TRIGGERS]


def _publish_needs() -> set[str]:
    """The jobs `build-and-publish` waits for, in either YAML list shape.

    Anchored to the job rather than to the first `needs:` in the file: that
    was correct only by declaration order, and a `needs:` added to any job
    above `build-and-publish` would silently become the list this test checked.
    """
    job = _jobs("release.yml")[PUBLISH_JOB]
    needs = job.get("needs")
    if isinstance(needs, str):
        needs = [needs]
    assert isinstance(needs, list) and needs, (
        f"release.yml: `{PUBLISH_JOB}` has a `needs:` this test cannot read as a gate: "
        f"{needs!r}. An absent or empty gate reads as a satisfied one."
    )
    assert all(isinstance(item, str) for item in needs), (
        f"release.yml: `{PUBLISH_JOB}`'s `needs:` holds something that is not a job id: {needs!r}"
    )
    return set(needs)


def test_the_tag_gate_is_not_weaker_than_the_pr_gate() -> None:
    """Every job a pull request runs has a release.yml job, gating the publish
    and running at least the same commands (finding M3).

    Exactly what is asserted, and nothing beyond it: for each workflow carrying
    a `pull_request:` (or `pull_request_target:`) trigger, every job id either
    maps to a job in release.yml (by id, or through RELEASE_GATE_ALIASES) or is
    named in RELEASE_GATE_EXEMPT; that counterpart's `run:` commands are a
    superset of the PR job's, minus the commands named in RELEASE_STEP_EXEMPT;
    and `build-and-publish`'s `needs:` names every other release.yml job and no
    job that does not exist. Job names, step names, `uses:` steps, matrix legs,
    `if:` conditions, `env:`, `continue-on-error:` and step order are NOT
    compared — the claim is about which commands run before a wheel is
    published, not about the two files being one file.

    A command is compared as the text PyYAML produces for the `run:` scalar,
    stripped of leading and trailing whitespace. So the spelling is free —
    plain, quoted, literal block, folded block, a plain scalar continued over
    several lines — and only what the shell would actually receive is compared.
    Shapes a real parse still cannot make sense of fail naming the file: a job
    with no `steps:` list, a `run:` that is not a non-empty string, a `needs:`
    that is not job ids, a duplicate mapping key, a file that does not parse.

    The parser under all of this is PyYAML rather than the regexes that used to
    be here, because those mis-read these files silently in three consecutive
    reviews — see this module's docstring. A step this test cannot see is a step
    a release can drop unnoticed, which is why RELEASE_STEP_EXEMPT exists:
    dropping one is a decision, spelled out, not a silence.

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

    release_jobs = set(_jobs("release.yml"))
    assert PUBLISH_JOB in release_jobs, f"release.yml has no `{PUBLISH_JOB}` job"
    needs = _publish_needs()

    missing: list[str] = []
    weaker: list[str] = []
    for workflow in pr_workflows:
        for job in sorted(_jobs(workflow)):
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
