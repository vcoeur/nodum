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

The second lock, ``test_the_tag_gate_is_not_weaker_than_the_pr_gate``, has no
script to shell out to: it reads the workflow files here, by hand, rather than
add a YAML dependency to the dev group for one assertion. Hand-parsing is
bought at a price, and the price is paid in refusals — every helper below
lists the shapes it reads, and anything else fails naming the file and line.
That rule is not fussiness: a `run: |` step used to match the one-line `run:`
pattern and be recorded as the command ``"|"``, which made two different
scripts compare equal, and a workflow read wrong is worse than one not read at
all because it reads green.
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

#: The header a key writes when its value is a block scalar: the style (`|`
#: literal, `>` folded) and the indicators that may follow it (chomping `-`/`+`,
#: an explicit indentation digit). Captured rather than swallowed so the shapes
#: this parser does not read can be refused by name: `run: |` used to match the
#: one-line `run:` pattern and be recorded as the command `"|"`, so a step's
#: whole body was dropped and two unrelated scripts compared equal.
_BLOCK_SCALAR = re.compile(r"(?P<style>[|>])(?P<indicators>[0-9+-]*)")

#: Characters a YAML plain scalar cannot begin with. Each opens a shape with
#: reading rules of its own — alias `*`, anchor `&`, tag `!`, flow collection
#: `{`/`[`, comment `#` — so a value starting with one is refused by name
#: rather than taken for literal text. Quotes and block scalars open shapes of
#: their own too, and those two are read: `_inline_command`,
#: `_block_scalar_command`.
_NOT_PLAIN = "*&!%@`{}[],#"


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
    found: int | None = None
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
        # A duplicated top-level key has no one right answer — PyYAML keeps the
        # last of the two, this scan used to return the first, and either way
        # every job under the other one is invisible. Refusing is the answer.
        if found is not None:
            raise AssertionError(
                f"{source} lines {found + 1} and {index + 1}: two top-level `{key}:` keys. "
                "Which one wins is not a question this parser answers quietly."
            )
        found = index
    assert found is not None, f"{source} has no top-level `{key}:` key"
    return found


def _nested_lines(lines: list[str], header_index: int) -> list[str]:
    """Every line nested under the key at ``header_index``.

    A block sequence may be written at its key's own indentation — `steps:`
    with its `- uses:` items in the same column — which is YAML's rule and not
    the style these workflows use. Reading those items as the *next* key's
    problem would leave `steps:` looking empty and a job looking like it runs
    nothing, so they are taken as the body they are.
    """
    depth = _indent(lines[header_index])
    body: list[str] = []
    for line in lines[header_index + 1 :]:
        if not _is_ignorable(line) and _indent(line) <= depth:
            stripped = line.strip()
            if _indent(line) < depth or not (stripped == "-" or stripped.startswith("- ")):
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
        assert match.group("key") not in keys, (
            f"{source} line {index + 1}: `{match.group('key')}:` is declared twice under "
            f"{lines[header_index].strip()!r}. Keeping the second would hide the first — "
            "a job in the file that is not in this test."
        )
        keys[match.group("key")] = index
    return keys


def _job_ids(name: str) -> set[str]:
    """The job ids one workflow declares."""
    lines = _workflow_lines(name)
    jobs = set(_child_keys(lines, _top_level_key(lines, "jobs", name), name))
    assert jobs, f"{name} declares no jobs — the parser read its `jobs:` block as empty"
    return jobs


def _job_lines(name: str, job_id: str) -> tuple[list[str], int]:
    """One job's body, and the index in the file its first line sits at.

    The offset is what turns a position inside the body back into the file
    line a failure has to name.
    """
    lines = _workflow_lines(name)
    jobs = _child_keys(lines, _top_level_key(lines, "jobs", name), name)
    assert job_id in jobs, f"{name} has no `{job_id}` job"
    return _nested_lines(lines, jobs[job_id]), jobs[job_id] + 1


def _location(source: str, offset: int, index: int) -> str:
    """``<file> line N`` for the line at ``index`` of a body starting at ``offset``."""
    return f"{source} line {offset + index + 1}"


def _block_scalar_extent(lines: list[str], header_index: int, column: int) -> tuple[list[str], int]:
    """The body of the block scalar opened at ``header_index``, and the index past it.

    A block scalar ends at the first non-blank line indented no further than
    the key that opened it — hence ``column``, that key's own indentation.
    Everything between is text, not structure: a blank line is content, a `#`
    line is content, and a line reading `run: ...` is part of the command
    rather than a step of its own.
    """
    end = header_index + 1
    while end < len(lines) and (not lines[end].strip() or _indent(lines[end]) > column):
        end += 1
    return lines[header_index + 1 : end], end


def _block_scalar_command(header: str, body: list[str], where: str) -> str:
    """The text of a literal block scalar — `run: |`, `run: |-`, `run: |+`.

    The body is de-indented by the indentation of its first non-blank line
    (YAML's own rule), joined with newlines, and stripped of leading and
    trailing blank lines. That last step is why the chomping indicator needs no
    handling of its own: two scripts differing only in a trailing newline are
    the same check.

    Folded scalars (`>`, `>-`, `>+`) and explicit indentation indicators
    (`|2`) are refused, naming the file and line. Folding — a break becoming a
    space here, staying a break there, blank lines counted — is a second parser
    this file is not going to grow, and an indentation indicator moves the
    de-indent column; getting either subtly wrong would put back exactly what
    this function exists to remove, a `run:` step read as something it is not.
    """
    style, indicators = header[0], header[1:]
    assert style == "|", (
        f"{where}: `{header}` is a folded block scalar. This parser reads literal block "
        "scalars (`|`, `|-`, `|+`) and refuses folded ones rather than guess where their "
        "line breaks become spaces — write the command as `|`, or teach the folding "
        "rules here."
    )
    assert not any(character.isdigit() for character in indicators), (
        f"{where}: `{header}` carries an explicit indentation indicator. This parser "
        "takes the block's indentation from its first line; write it that way, or teach "
        "the indicator here."
    )
    first = next((line for line in body if line.strip()), None)
    assert first is not None, f"{where}: `{header}` opens a block scalar with no content."
    content_indent = _indent(first)
    for line in body:
        assert not line.strip() or _indent(line) >= content_indent, (
            f"{where}: {line.strip()!r} is indented less than the first line of the block "
            "scalar it belongs to. That is not a block this parser can de-indent."
        )
    text = "\n".join(line[content_indent:] if line.strip() else "" for line in body)
    return text.strip("\n")


def _inline_command(value: str, where: str) -> str:
    """A `run:` value written on the key's own line — plain, or quoted.

    A plain value ends where YAML ends it, at the ` #` that opens a comment. A
    quoted value is unquoted when its quote closes on the same line and, for
    `"..."`, carries no backslash escape to decode. Everything else is refused
    by name: a quote left open is a multi-line flow scalar this parser would
    truncate, and an alias, anchor, tag or flow collection is a value whose
    text lives somewhere else entirely.
    """
    quote = value[0]
    if quote in "'\"":
        if quote == '"':
            close = -1 if "\\" in value else value.find('"', 1)
        else:
            close = 1
            while True:
                close = value.find("'", close)
                if close == -1 or value[close + 1 : close + 2] != "'":
                    break
                close += 2  # '' is an escaped quote, not the end of the scalar
        assert close != -1, (
            f"{where}: this parser reads a quoted `run:` only when the quote closes on "
            f"the same line and needs no escape decoding — {value!r} does neither. Write "
            "it plain, or as a `|` block."
        )
        trailer = value[close + 1 :].strip()
        assert not trailer or trailer.startswith("#"), (
            f"{where}: {trailer!r} follows the closing quote of {value!r}; a comment is "
            "the only thing this parser reads there."
        )
        text = value[1:close]
        return text.replace("''", "'") if quote == "'" else text
    assert quote not in _NOT_PLAIN, (
        f"{where}: {value!r} opens a shape this parser does not read — an alias, an "
        "anchor, a tag, a flow collection or a comment. A `run:` whose text lives "
        "somewhere else is a command this comparison would get wrong."
    )
    comment = value.find(" #")
    return (value if comment == -1 else value[:comment]).strip()


def _steps_lines(name: str, job_id: str) -> tuple[list[str], int]:
    """One job's `steps:` sequence, and the index in the file its first line sits at.

    A job whose steps this parser cannot find fails here rather than reading as
    a job that runs nothing: a `uses:` job calling a reusable workflow and a
    `steps: *anchor` are both lists of checks that would otherwise be invisible
    to the comparison below. Either sequence indentation is read; see
    `_nested_lines`.
    """
    body, offset = _job_lines(name, job_id)
    child_indent: int | None = None
    for index, line in enumerate(body):
        if _is_ignorable(line):
            continue
        indent = _indent(line)
        if child_indent is None:
            child_indent = indent
        if indent != child_indent:
            continue
        match = _KEY.fullmatch(line)
        if match is None or match.group("key") != "steps":
            continue
        assert not match.group("value").strip(), (
            f"{_location(name, offset, index)}: `steps:` is written as {line.strip()!r}; "
            "this parser reads only the block form. An alias or a flow sequence here is "
            "a list of checks it cannot see."
        )
        steps = _nested_lines(body, index)
        assert any(not _is_ignorable(step) for step in steps), (
            f"{_location(name, offset, index)}: `steps:` opens an empty block — a job "
            "with no steps at all, or a shape this parser reads as none."
        )
        return steps, offset + index + 1
    raise AssertionError(
        f"{name}'s `{job_id}` job declares no `steps:` this parser can find. A job that "
        "calls a reusable workflow with `uses:` runs checks this comparison cannot read; "
        "teach the parser rather than let them go uncompared."
    )


def _job_run_steps(name: str, job_id: str) -> list[str]:
    """The shell commands one job runs, in order.

    `run:` steps only: `uses:` steps name actions, and comparing those across
    two workflows would compare checkout pins rather than checks.

    Read: `- run: cmd` and `run: cmd` (the named-step shape), plain or quoted,
    with or without a trailing `#` comment, and the literal block scalars
    `run: |`, `run: |-`, `run: |+`, whose bodies are de-indented and joined
    with newlines. A block scalar's body is consumed as text, so a
    `run:`-looking line inside a heredoc belongs to that command instead of
    becoming a step, and a block scalar under any other key (`if: |`) is
    skipped whole.

    Refused, each naming the file and line: a folded `run: >`, an explicit
    indentation indicator (`run: |2`), a quote that does not close on the line,
    an alias, an anchor, a tag, a flow collection, a `run:` with nothing after
    it, and a `run:` at a depth other than the one this job's steps sit at.
    Nothing is skipped: `run: |` used to match the old one-line pattern and be
    recorded as the command `"|"`, so a step's whole body vanished and two
    unrelated scripts compared equal — a release could have dropped either.
    """
    steps, offset = _steps_lines(name, job_id)
    commands: list[str] = []
    step_column: int | None = None
    index = 0
    while index < len(steps):
        line = steps[index]
        index += 1
        if _is_ignorable(line):
            continue
        where = _location(name, offset, index - 1)
        column = _indent(line)
        rest = line[column:]
        while rest.startswith("-") and rest[1:2] in ("", " "):
            # A sequence marker (`- `, or a bare `-` with the mapping below it)
            # moves the key's column right by as much as it and its padding take.
            marker = len(rest) - len(rest[1:].lstrip(" "))
            column += marker
            rest = rest[marker:]
        if not rest:
            continue
        if step_column is None:
            step_column = column
        match = _KEY.fullmatch(rest)
        if match is None:
            # A scalar sequence entry (`- "docs/**"`) or the continuation of a
            # multi-line plain scalar: no key, so no step. A plain scalar cannot
            # contain `: `, so no continuation line can look like one either.
            assert rest[0] not in _NOT_PLAIN, (
                f"{where}: {rest!r} opens a shape this parser does not read — an alias, "
                "an anchor, a tag or a flow collection. A step written as a flow mapping "
                "(`- {run: ...}`) is a step it would never see."
            )
            continue
        key, value = match.group("key"), match.group("value").strip()
        block = value[:1] in ("|", ">")
        header, body = "", []
        if block:
            header = value.split(" ", 1)[0]
            trailer = value[len(header) :].strip()
            assert _BLOCK_SCALAR.fullmatch(header) and (not trailer or trailer.startswith("#")), (
                f"{where}: {value!r} starts like a block scalar but is not a header this "
                "parser recognises (`|`, `>`, a chomping indicator, an indentation digit, "
                "then a comment at most)."
            )
            body, index = _block_scalar_extent(steps, index - 1, column)
        if key != "run":
            continue
        assert column == step_column, (
            f"{where}: a `run:` key at column {column}, where this job's steps start at "
            f"{step_column}. If it is a step, indent it like the others; if it is an "
            "input to an action (`with:`), this parser has to be taught the difference "
            "rather than count it as a command."
        )
        if block:
            commands.append(_block_scalar_command(header, body, where))
            continue
        assert value, (
            f"{where}: `run:` with nothing after it and no block scalar opened. An empty "
            "step is either a mistake or a shape this parser does not read."
        )
        commands.append(_inline_command(value, where))
    return commands


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
    body, _ = _job_lines("release.yml", PUBLISH_JOB)
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

    A command is compared as text, read by `_job_run_steps`: a one-line `run:`
    (plain or quoted, comment stripped) or a literal block scalar (`|`, `|-`,
    `|+`) de-indented and joined with newlines, so `run: cmd` and a one-line
    `run: |` block of the same command are the same command, and only a
    trailing-newline difference is normalised away. Every other shape — a
    folded `>`, an indentation indicator, an unclosed quote, an alias, a flow
    mapping, a job whose `steps:` cannot be found — fails naming the file and
    line instead of being read wrong or skipped. A step this parser cannot see
    is a step a release can drop unnoticed, which is why RELEASE_STEP_EXEMPT
    exists: dropping one is a decision, spelled out, not a silence.

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
