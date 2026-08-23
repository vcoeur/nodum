"""The settings export: the dialect, the redacted default, and the round trip.

Four properties carry this file, mirroring the store's own:

**The consumer is docker compose, not nodum.** The exported file is parsed
back by `docker compose config` (or python-dotenv where docker is absent)
and compared against the *effective* settings — never nodum's parser against
itself, which is the round trip that cannot fail.

**The export renders what runs.** An environment-pinned key is emitted with
its pinned value: freezing the live configuration is the verb's job.

**Redacted is the default.** Without ``--include-secrets`` a secret becomes
an omission comment; its value reaches no file, no stdout, no error body.

**The file works in a process that has never seen this one.** The
fresh-process test feeds an exported file to a brand-new interpreter whose
provider resolves from it alone — the test that would have caught a
value-stripping defect.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest
from helpers import owner
from typer.testing import CliRunner

from nodum import cli, service, settings

runner = CliRunner()

#: A value long enough that a partial write would be visible, and distinctive
#: enough that grepping for it in a whole CLI output means something.
SECRET = "sk-test-000111222333444555666777888999"

#: A value that hits every rule in the dialect at once: dollar (interpolation),
#: double quote (the quoting character), backslash (the escape character),
#: a hash (inline-comment bait) and padding whitespace (unquoted trimming).
NASTY_VALUE = 'deep "$3" \\2 #tag  padded  '


def _run_json(*args: str) -> dict:
    result = runner.invoke(cli.app, list(args))
    assert result.exit_code == 0, result.output + result.stderr
    return json.loads(result.output)


def _store_tricky_configuration(monkeypatch) -> None:
    """Plant values that exercise every escaping rule, one per layer."""
    monkeypatch.setenv(settings.LLM_BASE_URL, "https://api.example.com/v1")
    settings.set_value(settings.LLM_MODEL, NASTY_VALUE)
    settings.set_value(settings.LLM_API_KEY, SECRET)
    settings.set_value(settings.LLM_CONTEXT_TOKENS, "262144")


# ── The dialect ───────────────────────────────────────────────────────────────


def test_redacted_export_omits_the_secret_and_names_it(fresh_db):
    settings.set_value(settings.LLM_API_KEY, SECRET)
    text = settings.render_env_file()
    assert SECRET not in text
    assert f"# {settings.LLM_API_KEY} is set but omitted by this redacted export" in text
    # A non-secret key still carries its value.
    assert f"{settings.LLM_MODEL}=" in text or "# NODUM_LLM_MODEL is not set" in text


def test_include_secrets_emits_the_value(fresh_db):
    settings.set_value(settings.LLM_API_KEY, SECRET)
    text = settings.render_env_file(include_secrets=True)
    assert f'{settings.LLM_API_KEY}="{SECRET}"' in text


def test_export_renders_effective_values_environment_included(fresh_db, monkeypatch):
    """R2-S8: an env-pinned key is exported with its pinned value."""
    monkeypatch.setenv(settings.LLM_MODEL, "env-pinned-model")
    settings.set_value(settings.LLM_CONTEXT_TOKENS, "4096")
    text = settings.render_env_file(include_secrets=True)
    assert f'{settings.LLM_MODEL}="env-pinned-model"' in text
    assert f'{settings.LLM_CONTEXT_TOKENS}="4096"' in text


_COMPOSE_FILE = """\
services:
  probe:
    image: busybox
    environment:
{entries}
"""


def test_round_trip_through_docker_compose(fresh_db, monkeypatch, tmp_path):
    """The gate the plan demands: `docker compose config` reads the file back.

    Skipped where docker is absent; `test_round_trip_through_python_dotenv`
    below is the CI fallback. Neither parser is nodum's.
    """
    if shutil.which("docker") is None:
        pytest.skip("docker is not available")
    _store_tricky_configuration(monkeypatch)
    export = tmp_path / "exported.env"
    export.write_text(
        service.export_settings(principal=owner(), include_secrets=True), encoding="utf-8"
    )

    names = [name for name in settings.KEYS]
    entries = "\n".join(f"      {name}: ${{{name}}}" for name in names)
    compose = tmp_path / "compose.yml"
    compose.write_text(_COMPOSE_FILE.format(entries=entries), encoding="utf-8")

    done = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(compose),
            "--env-file",
            str(export),
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    environment = json.loads(done.stdout)["services"]["probe"]["environment"]

    # `compose config` serialises for round-tripping: it re-escapes each
    # dollar as `$$` in its own output (the same rule its dotenv reader
    # applies to *unquoted* text in a .env file). Undoing that one
    # serialization escape recovers the raw value; nothing else is escaped.
    def unescaped(value: str) -> str:
        return value.replace("$$", "$")

    effective = settings.snapshot()
    for name in names:
        expected = effective.value(name)
        if expected is None:
            continue
        actual = environment.get(name)
        assert actual is not None, name
        assert unescaped(actual) == expected, name


def test_round_trip_through_python_dotenv(fresh_db, monkeypatch, tmp_path):
    """The docker-absent fallback, with one documented difference.

    python-dotenv processes the backslash and quote escapes exactly as
    compose does and has **no interpolator** — matching compose's own
    literal treatment of double-quoted values — so the parsed values compare
    as they stand.
    """
    dotenv = pytest.importorskip("dotenv")
    _store_tricky_configuration(monkeypatch)
    export = tmp_path / "exported.env"
    export.write_text(
        service.export_settings(principal=owner(), include_secrets=True), encoding="utf-8"
    )

    parsed = dotenv.dotenv_values(export)
    effective = settings.snapshot()
    for name in settings.KEYS:
        expected = effective.value(name)
        if expected is None:
            continue
        actual = parsed.get(name)
        assert actual is not None, name
        assert actual == expected, name


def test_fresh_process_resolves_the_provider_from_the_exported_file(
    fresh_db, monkeypatch, tmp_path
):
    """The whole point of the dialect: a new interpreter lives off the file.

    The child loads the export into its own environment (python-dotenv, not
    nodum's parser), then asks :func:`nodum.llm.resolution` to build a
    provider from it alone — the check that fails if the export stripped or
    mangled a value the provider needs.
    """
    monkeypatch.setenv(settings.LLM_BASE_URL, "https://api.example.com/v1")
    settings.set_value(settings.LLM_MODEL, "deepseek-chat")
    settings.set_value(settings.LLM_API_KEY, SECRET)
    export = tmp_path / "exported.env"
    export.write_text(
        service.export_settings(principal=owner(), include_secrets=True), encoding="utf-8"
    )

    program = (
        "import sys\n"
        "from dotenv import dotenv_values\n"
        "for key, value in dotenv_values(sys.argv[1]).items():\n"
        "    if value is not None:\n"
        "        import os\n"
        "        os.environ[key] = value\n"
        "from nodum import llm, settings\n"
        "settings.reset()\n"
        "resolution = llm.resolution()\n"
        "print('RESOLVED' if resolution.provider is not None else 'REFUSED')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", program, str(export)],
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert done.returncode == 0, done.stderr
    assert "RESOLVED" in done.stdout, done.stdout + done.stderr


# ── The CLI verb ──────────────────────────────────────────────────────────────


def test_cli_export_is_redacted_by_default(fresh_db, tmp_path, monkeypatch, capsys):
    _store_tricky_configuration(monkeypatch)
    out = tmp_path / "out.env"
    result = runner.invoke(cli.app, ["config", "export", "--out", str(out), "--as", "owner"])
    assert result.exit_code == 0, result.output + result.stderr
    payload = json.loads(result.output)
    assert payload["include_secrets"] is False
    text = out.read_text(encoding="utf-8")
    assert SECRET not in text
    assert result.stdout.find(SECRET) == -1
    assert payload["count"] > 0


def test_cli_export_with_secrets_writes_the_value(fresh_db, tmp_path, monkeypatch):
    _store_tricky_configuration(monkeypatch)
    out = tmp_path / "out.env"
    result = runner.invoke(
        cli.app,
        ["config", "export", "--out", str(out), "--include-secrets", "--as", "owner"],
    )
    assert result.exit_code == 0, result.output + result.stderr
    payload = json.loads(result.output)
    assert payload["include_secrets"] is True
    assert f'{settings.LLM_API_KEY}="{SECRET}"' in out.read_text(encoding="utf-8")


def test_cli_export_appends_one_event_with_the_flag_and_no_value(fresh_db, tmp_path):
    out = tmp_path / "out.env"
    result = runner.invoke(cli.app, ["config", "export", "--out", str(out), "--as", "owner"])
    assert result.exit_code == 0, result.output + result.stderr
    events = [
        row
        for row in service.list_events(limit=50, principal=owner())
        if row.op == "settings.export"
    ]
    assert len(events) == 1
    assert events[0].actor == "human:owner"
    assert events[0].payload == {"include_secrets": False}
    assert SECRET not in json.dumps([row.payload for row in events])


def test_cli_export_writes_the_file_at_0600(fresh_db, tmp_path, monkeypatch):
    """A credential-bearing export never lands world-readable — even redacted.

    Both ways: a fresh file (the create case) and a re-export over an existing
    0644 one (O_CREAT's mode argument applies at creation only).
    """
    import os as os_module
    import stat

    _store_tricky_configuration(monkeypatch)
    for flags in ([], ["--include-secrets"]):
        out = tmp_path / f"out-{'keys' if flags else 'redacted'}.env"
        out.write_text("stale", encoding="utf-8")
        os_module.chmod(out, 0o644)
        result = runner.invoke(
            cli.app, ["config", "export", "--out", str(out), *flags, "--as", "owner"]
        )
        assert result.exit_code == 0, result.output + result.stderr
        assert stat.S_IMODE(out.stat().st_mode) == 0o600, flags


def test_cli_export_refuses_an_agent_principal(fresh_db, tmp_path):
    from helpers import agent as agent_principal

    agent_principal("no-export")
    out = tmp_path / "out.env"
    result = runner.invoke(
        cli.app, ["config", "export", "--out", str(out), "--as", "agent:no-export"]
    )
    assert result.exit_code != 0
    assert not out.exists()
