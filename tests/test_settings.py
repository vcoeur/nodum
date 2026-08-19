"""The settings store: the ladder, the dialect, the write path, and what stays secret.

Four properties carry this file, and they are the four a second configuration
layer has to have before anything may depend on it.

**The ladder is right on the deployment that exists.** The container this ships
into renders six of its variables as ``${VAR:-}`` pass-throughs and one as a
bare interpolation, so seven names are *present and empty* in ``os.environ``. A
precedence keyed on presence would pin all seven to an empty string and make
the file unreachable, which is why ``deployed_environment`` is a fixture rather
than a footnote: it is the exact shape the precedence rule has to survive.

**A write is atomic, private, and durable.** 0600 at creation rather than after,
an ``O_EXCL`` temp file in the same directory, ``fsync`` on the file and on the
directory, and one writer at a time across processes. Each of those is asserted
here rather than described in a docstring, because every one of them is
invisible until the day it is not.

**A change is live.** Two of the three classes of liveness the design promises
are here — the next run's budget and the provider's model — driven through the
store rather than through ``os.environ``, and the model one driven from a
*second process*, which is the only version of that claim worth anything. The
third, the nightly schedule, needs the loop and so lives in
``tests/test_scheduler.py``.

**A secret never leaves.** The sweep at the end runs the real CLI and reads
stdout, stderr and the event log for the key it just stored.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from helpers import owner
from typer.testing import CliRunner

from nodum import agent, cli, db, embeddings, extract, llm, service, settings, urls

runner = CliRunner()

#: A value long enough that a partial write would be visible, and distinctive
#: enough that grepping for it in a whole CLI output means something.
SECRET = "sk-test-000111222333444555666777888999"


def _run_json(*args: str) -> dict:
    """Invoke the CLI and parse its one JSON object, failing loudly on a non-zero exit."""
    result = runner.invoke(cli.app, list(args))
    assert result.exit_code == 0, result.output + result.stderr
    return json.loads(result.stdout)


@pytest.fixture()
def store(tmp_path) -> Path:
    """A bound settings store in a directory of its own; returns the settings path."""
    settings.reset()
    path = settings.bind(tmp_path / "graph.db")
    assert path is not None
    return path


@pytest.fixture()
def deployed_environment(monkeypatch):
    """The container's environment, as ``origin/main``'s compose file renders it.

    Three hardcoded literals, six ``${VAR:-}`` pass-throughs that render
    **present and empty** when the host ``.env`` says nothing, and the bare
    ``NODUM_PUBLIC_URL`` interpolation which renders present-and-empty the same
    way. Ten of the nineteen names are present in the container and seven of
    those are empty. This is the fixture the precedence rule has to survive, and
    it is why presence is not the signal.
    """
    for name, value in {
        settings.DB: "/data/nodum.db",
        settings.EMBED_CACHE: "/models",
        "NODUM_EMBED_DOWNLOAD": "1",
    }.items():
        monkeypatch.setenv(name, value)
    for name in (
        settings.LLM_MODEL,
        settings.LLM_BASE_URL,
        settings.LLM_API_KEY,
        settings.LLM_CONTEXT_TOKENS,
        settings.LLM_CYCLE_BUDGET,
        settings.CONSOLIDATE_AT,
        settings.PUBLIC_URL,
    ):
        monkeypatch.setenv(name, "")
    for name in (
        settings.LLM_THINKING,
        settings.LLM_CYCLE_SECONDS,
        settings.LLM_REQUEST_BUDGET,
        settings.LLM_REQUEST_SECONDS,
        settings.LLM_CALL_TIMEOUT,
        settings.LLM_MAX_OUTPUT_TOKENS,
        settings.AUDIO_MODEL,
        settings.AUDIO_DOWNLOAD,
    ):
        monkeypatch.delenv(name, raising=False)


# ── The registry says the same thing as the modules that read it ──────────────


def test_every_key_the_registry_names_is_the_constant_its_module_reads(store):
    """One name per setting, or a rename lands in one place and not the other.

    The registry cannot import the modules that own these constants — the
    provider seam may not reach back into the package — so the two spellings
    are checked against each other here instead of being derived from one.
    """
    assert settings.DB == db.ENV_DB_VAR
    assert settings.PUBLIC_URL == urls.PUBLIC_URL_ENV
    assert settings.LLM_MODEL == llm.ENV_MODEL
    assert settings.LLM_BASE_URL == llm.ENV_BASE_URL
    assert settings.LLM_API_KEY == llm.ENV_API_KEY
    assert settings.LLM_CONTEXT_TOKENS == llm.ENV_CONTEXT_TOKENS
    assert settings.LLM_THINKING == llm.ENV_THINKING
    assert settings.LLM_CYCLE_BUDGET == agent.ENV_CYCLE_BUDGET
    assert settings.LLM_CYCLE_SECONDS == agent.ENV_CYCLE_SECONDS
    assert settings.LLM_REQUEST_BUDGET == agent.ENV_REQUEST_BUDGET
    assert settings.LLM_REQUEST_SECONDS == agent.ENV_REQUEST_SECONDS
    assert settings.LLM_CALL_TIMEOUT == agent.ENV_CALL_TIMEOUT
    assert settings.LLM_MAX_OUTPUT_TOKENS == agent.ENV_MAX_OUTPUT_TOKENS
    assert settings.EMBED_CACHE == embeddings.ENV_CACHE_VAR
    assert settings.AUDIO_MODEL == extract.ENV_AUDIO_MODEL_VAR
    assert settings.AUDIO_DOWNLOAD == extract.ENV_AUDIO_DOWNLOAD_VAR


def test_every_default_the_registry_reports_is_the_default_the_runtime_uses(store):
    """A reported default that is not the real one is worse than none reported.

    ``nodum config list`` answers "what happens if I unset this", and an
    operator acts on that answer. Each line here is the same number read from
    the module that actually falls back to it.
    """
    default = {name: settings.SPECS[name].default for name in settings.KEYS}
    # `db.DEFAULT_DB_PATH` is redirected for the whole session by
    # `_never_the_real_database`, so it is the one default that cannot be
    # compared against its module; it is checked against the literal the docs
    # publish instead, which is the claim a reader acts on.
    assert default[settings.DB] == "~/.local/share/nodum/nodum.db"
    assert default[settings.PUBLIC_URL] == urls.DEFAULT_PUBLIC_URL
    assert default[settings.LLM_BASE_URL] == llm.DEFAULT_BASE_URL
    assert default[settings.LLM_CONTEXT_TOKENS] == str(llm.DEFAULT_CONTEXT_TOKENS)
    assert default[settings.LLM_THINKING] == llm.DEFAULT_THINKING
    assert default[settings.LLM_CYCLE_BUDGET] == str(agent.DEFAULT_CYCLE_BUDGET)
    assert float(default[settings.LLM_CYCLE_SECONDS]) == agent.DEFAULT_CYCLE_SECONDS
    assert default[settings.LLM_REQUEST_BUDGET] == str(agent.DEFAULT_REQUEST_BUDGET)
    assert float(default[settings.LLM_REQUEST_SECONDS]) == agent.DEFAULT_REQUEST_SECONDS
    assert float(default[settings.LLM_CALL_TIMEOUT]) == agent.DEFAULT_CALL_TIMEOUT
    assert default[settings.LLM_MAX_OUTPUT_TOKENS] == str(agent.DEFAULT_MAX_OUTPUT_TOKENS)
    assert Path(default[settings.EMBED_CACHE]).expanduser() == embeddings.DEFAULT_CACHE_PATH
    assert default[settings.AUDIO_MODEL] == extract.AUDIO_MODEL
    # The three that are deliberately *off* when nothing sets them.
    assert default[settings.LLM_MODEL] is None
    assert default[settings.LLM_API_KEY] is None
    assert default[settings.CONSOLIDATE_AT] is None
    assert default[settings.AUDIO_DOWNLOAD] is None


# ── The ladder ────────────────────────────────────────────────────────────────


def test_the_file_beats_a_present_but_empty_environment_variable(store, deployed_environment):
    """The deployed shape, and the whole reason precedence cannot key on presence.

    Six of the container's variables are exported empty. If "present" meant
    "set", storing a model here would be accepted, reported, and never used.
    """
    assert settings.LLM_MODEL in os.environ
    assert settings.resolve(settings.LLM_MODEL) is None

    settings.set_value(settings.LLM_MODEL, "qwen3:8b")

    assert settings.resolve(settings.LLM_MODEL) == "qwen3:8b"
    assert settings.provenance(settings.LLM_MODEL) == settings.FROM_FILE


def test_a_non_empty_environment_variable_beats_the_file(store, deployed_environment, monkeypatch):
    """Env wins, so a deployment can pin a value the browser cannot move."""
    settings.set_value(settings.LLM_MODEL, "qwen3:8b")
    monkeypatch.setenv(settings.LLM_MODEL, "deepseek-chat")

    assert settings.resolve(settings.LLM_MODEL) == "deepseek-chat"
    assert settings.provenance(settings.LLM_MODEL) == settings.FROM_ENVIRONMENT
    # And the stored value is still there, waiting for the pin to be removed.
    assert settings.stored_values()[settings.LLM_MODEL] == "qwen3:8b"


@pytest.mark.parametrize(
    ("key", "stored", "expected"),
    [
        (settings.LLM_MODEL, "qwen3:8b", "qwen3:8b"),
        (settings.LLM_CONTEXT_TOKENS, "8192", "8192"),
        (settings.LLM_CYCLE_SECONDS, "900.5", "900.5"),
        (settings.LLM_THINKING, "LOW", "low"),
        (settings.CONSOLIDATE_AT, "03:30", "03:30"),
        (settings.AUDIO_DOWNLOAD, "1", "1"),
        (settings.AUDIO_MODEL, "small", "small"),
    ],
)
def test_the_precedence_matrix_holds_for_every_type(store, monkeypatch, key, stored, expected):
    """Default, then file, then environment — one row per kind of value there is."""
    monkeypatch.delenv(key, raising=False)
    default = settings.SPECS[key].default
    assert settings.resolve(key) == default

    settings.set_value(key, stored)
    assert settings.resolve(key) == expected
    assert settings.provenance(key) == settings.FROM_FILE

    monkeypatch.setenv(key, "from-the-environment")
    assert settings.resolve(key) == "from-the-environment"

    monkeypatch.setenv(key, "   ")
    assert settings.resolve(key) == expected, "whitespace is not a value at any layer"

    monkeypatch.delenv(key)
    settings.unset_value(key)
    assert settings.resolve(key) == default
    assert settings.provenance(key) in (settings.FROM_DEFAULT, settings.FROM_UNSET)


def test_an_empty_audio_model_is_the_default_and_not_the_model_named_empty(store, monkeypatch):
    """The two-argument ``os.environ.get`` hole, closed by going through the seam.

    ``NODUM_AUDIO_MODEL`` was read as ``os.environ.get(NAME, DEFAULT)``, so an
    exported-but-empty variable — the shape a compose ``${VAR:-}`` renders —
    became the model name ``""`` rather than the default, and faster-whisper
    was asked for a model called nothing.
    """
    monkeypatch.setenv(settings.AUDIO_MODEL, "")
    assert settings.resolve(settings.AUDIO_MODEL) == extract.AUDIO_MODEL

    monkeypatch.setenv(settings.AUDIO_DOWNLOAD, "")
    assert settings.resolve(settings.AUDIO_DOWNLOAD) is None


def test_a_snapshot_is_one_configuration_and_does_not_move(store):
    """A caller reading three values one at a time can act on a mixture of two."""
    settings.set_value(settings.LLM_CYCLE_BUDGET, "100")
    view = settings.snapshot()

    settings.set_value(settings.LLM_CYCLE_BUDGET, "900")

    assert view.value(settings.LLM_CYCLE_BUDGET) == "100"
    assert settings.snapshot().value(settings.LLM_CYCLE_BUDGET) == "900"
    assert view.generation != settings.generation()


def test_the_published_mapping_is_immutable_and_replaced_rather_than_mutated(store):
    """A caller holding one keeps a consistent view instead of watching it change."""
    settings.set_value(settings.LLM_CYCLE_BUDGET, "100")
    first = settings.stored_values()
    with pytest.raises(TypeError):
        first[settings.LLM_CYCLE_BUDGET] = "0"  # pyright: ignore[reportIndexIssue]

    settings.set_value(settings.LLM_CYCLE_BUDGET, "900")

    assert first[settings.LLM_CYCLE_BUDGET] == "100"
    assert settings.stored_values() is not first


def test_a_generation_is_never_reused_across_a_rebind(tmp_path):
    """Per-store counters both started at 0, so two graphs reached 1.

    A cache comparing a bare integer — ``nodum.llm``'s does — then saw
    "unchanged" across a rebind and went on serving the first graph's provider
    while every other surface had already followed. One counter for the process
    makes a generation unique to the reading that produced it.
    """
    settings.reset()
    first, second = tmp_path / "a", tmp_path / "b"
    settings.bind(first / "graph.db")
    settings.set_value(settings.LLM_MODEL, "from-a")
    seen = settings.generation()

    settings.bind(second / "graph.db")
    settings.set_value(settings.LLM_MODEL, "from-b")

    assert settings.resolve(settings.LLM_MODEL) == "from-b"
    assert settings.generation() != seen, "the second graph reused the first's generation"


def test_a_reader_does_not_wait_on_another_process_holding_the_write_lock(store, tmp_path):
    """The cache lock and the write lock are different locks, and this is why.

    Sharing one made every ``resolve`` wait on whatever process held the file.
    The readers include the scheduler's slice on the event loop and
    ``llm.resolution``, which asks while holding its own lock — so one stuck
    writer stalled the loop and every path to the model behind it.
    """
    settings.set_value(settings.LLM_CYCLE_BUDGET, "100")
    assert settings.resolve(settings.LLM_CYCLE_BUDGET) == "100"

    held, release = tmp_path / "held", tmp_path / "release"
    child = subprocess.Popen(
        [sys.executable, "-c", _HOLD_THE_LOCK, str(store) + ".lock", str(release), str(held)]
    )
    try:
        for _ in range(500):
            if held.exists():
                break
            threading.Event().wait(0.01)
        assert held.exists(), "the child never took the lock"

        started = time.perf_counter()
        assert settings.resolve(settings.LLM_CYCLE_BUDGET) == "100"
        elapsed = time.perf_counter() - started
    finally:
        release.write_text("go")
        child.wait(timeout=5)

    assert elapsed < 0.5, f"a read waited {elapsed:.2f}s on a foreign writer's lock"


def test_an_unchanged_file_is_stamped_without_being_read(store, monkeypatch):
    """The published cost sentence, made true rather than restated.

    "One stat per resolution" was three times wrong: the first cut fstat-ed and
    then read the whole file *before* comparing, so an unchanged resolve cost
    two reads and the file's bytes.
    """
    settings.set_value(settings.LLM_CYCLE_BUDGET, "100")
    settings.resolve(settings.LLM_CYCLE_BUDGET)

    reads: list[int] = []
    real_read = os.read

    def counting_read(descriptor: int, length: int) -> bytes:
        reads.append(descriptor)
        return real_read(descriptor, length)

    monkeypatch.setattr(os, "read", counting_read)
    assert settings.resolve(settings.LLM_CYCLE_BUDGET) == "100"

    assert reads == [], "an unchanged file was read rather than only stamped"


def test_a_refresh_between_two_loads_cannot_stamp_old_values_as_new(store):
    """Values and generation come out of one hold of the cache lock.

    Read left to right outside it, a refresh landing between the loads yielded
    the *old* values under the *new* generation — a cache holding that pair
    believes it is current forever.
    """
    settings.set_value(settings.LLM_CYCLE_BUDGET, "100")
    first = settings.snapshot()
    settings.set_value(settings.LLM_CYCLE_BUDGET, "900")
    second = settings.snapshot()

    assert (first.value(settings.LLM_CYCLE_BUDGET), second.value(settings.LLM_CYCLE_BUDGET)) == (
        "100",
        "900",
    )
    assert first.generation != second.generation
    # Every value in a snapshot came from the reading its generation names.
    again = settings.snapshot()
    assert again.generation == second.generation
    assert again.value(settings.LLM_CYCLE_BUDGET) == "900"


def test_free_text_keys_report_no_bad_value_posture(store):
    """There is no invalid value for a key nothing validates, so there is no rule.

    Reporting ``refuse`` against ``NODUM_LLM_MODEL`` described a check that does
    not exist: any non-empty string is accepted here *and* by the runtime, and a
    model nothing serves is an HTTP error on the first call.
    """
    unchecked = {name for name in settings.KEYS if settings.SPECS[name].validate is settings._text}
    assert unchecked == {
        settings.DB,
        settings.PUBLIC_URL,
        settings.LLM_MODEL,
        settings.LLM_BASE_URL,
        settings.LLM_API_KEY,
        settings.EMBED_CACHE,
        settings.AUDIO_MODEL,
    }
    for name in settings.KEYS:
        posture = settings.SPECS[name].on_invalid
        assert (posture is None) == (name in unchecked), name

    rows = {row.key: row.on_invalid for row in settings.list_settings().settings}
    assert {name for name, posture in rows.items() if posture is None} == unchecked


def test_a_schedule_carrying_a_utc_offset_is_refused(store):
    """``time.fromisoformat`` takes ``03:30+05:00``; a local wall clock does not.

    The scheduler reads the result as local wall time — the one thing an offset
    says it is not — so the cycle would run at 03:30 local under a setting
    asking for 03:30 elsewhere.
    """
    with pytest.raises(settings.SettingRefused, match="no UTC offset"):
        settings.set_value(settings.CONSOLIDATE_AT, "03:30+05:00")

    assert not store.exists()


def test_a_key_the_file_repeats_is_reported_once(store):
    """The message is a list of names, and a name said twice reads as two problems."""
    store.write_text("NODUM_FUTURE=a\nNODUM_FUTURE=b\nNODUM_OTHER=c\n", encoding="utf-8")

    assert settings.list_settings().unknown_keys == ["NODUM_FUTURE", "NODUM_OTHER"]


# ── The dialect ───────────────────────────────────────────────────────────────


def test_comments_blank_lines_and_unknown_keys_survive_a_rewrite(store):
    """The file is the operator's, and a rewrite that tidied it would lose their notes.

    An unknown key is the sharper half: it may belong to a newer nodum, and
    dropping it would turn an upgrade-then-downgrade into data loss.
    """
    store.write_text(
        "# the gardener's budget, raised for the December backfill\n"
        "\n"
        "NODUM_LLM_CYCLE_BUDGET=100\n"
        "NODUM_FUTURE_SETTING=keep-me\n",
        encoding="utf-8",
    )

    settings.set_value(settings.LLM_CYCLE_BUDGET, "900")

    text = store.read_text(encoding="utf-8")
    assert "# the gardener's budget, raised for the December backfill" in text
    assert "NODUM_FUTURE_SETTING=keep-me" in text
    assert "NODUM_LLM_CYCLE_BUDGET=900" in text
    assert "NODUM_LLM_CYCLE_BUDGET=100" not in text
    assert settings.list_settings().unknown_keys == ["NODUM_FUTURE_SETTING"]


def test_a_value_carrying_a_newline_is_refused_rather_than_written(store):
    """A newline in a value is not a formatting problem — it is line injection.

    Written verbatim, ``qwen3\\nNODUM_LLM_API_KEY=stolen`` is two settings, the
    second chosen by whoever supplied the value.
    """
    with pytest.raises(settings.SettingRefused, match="control characters"):
        settings.set_value(settings.LLM_MODEL, "qwen3\nNODUM_LLM_API_KEY=stolen")

    assert not store.exists()


@pytest.mark.parametrize("value", ["\x00", "a\tb", "a\rb", "a\x7fb"])
def test_no_control_character_reaches_the_file(store, value: str):
    with pytest.raises(settings.SettingRefused, match="control characters"):
        settings.set_value(settings.LLM_MODEL, value)


def test_a_file_it_cannot_parse_is_reported_and_stepped_around(store, caplog, monkeypatch):
    """Report loudly and continue: a server that will not answer over a stray
    character in an optional file is worse than one that says what it ignored."""
    monkeypatch.delenv(settings.LLM_CYCLE_BUDGET, raising=False)
    store.write_text("NODUM_LLM_CYCLE_BUDGET=100\nthis is not a setting\n", encoding="utf-8")

    with caplog.at_level("ERROR", logger="nodum.settings"):
        resolved = settings.resolve(settings.LLM_CYCLE_BUDGET)

    assert resolved == settings.SPECS[settings.LLM_CYCLE_BUDGET].default
    assert settings.provenance(settings.LLM_CYCLE_BUDGET) == settings.FROM_UNREADABLE
    assert settings.unreadable_reason() is not None
    assert "could not be parsed" in caplog.text


def test_a_write_against_an_unparseable_file_is_refused_rather_than_clobbering_it(store):
    """It cannot promise to preserve lines it cannot read, so it does not try."""
    store.write_text("NODUM_LLM_MODEL=qwen3:8b\n???\n", encoding="utf-8")

    with pytest.raises(settings.SettingsFileUnreadable, match="fix the file by hand"):
        settings.set_value(settings.LLM_CYCLE_BUDGET, "900")

    assert "NODUM_LLM_MODEL=qwen3:8b" in store.read_text(encoding="utf-8")


# ── The write path ────────────────────────────────────────────────────────────


def test_the_file_is_created_private_and_never_briefly_world_readable(store):
    """0600 at ``O_CREAT``, not chmod-ed after: this file can hold an API key."""
    settings.set_value(settings.LLM_API_KEY, SECRET)

    assert store.stat().st_mode & 0o777 == 0o600


def test_the_write_fsyncs_the_file_and_the_directory_it_was_renamed_into(store, monkeypatch):
    """``os.replace`` orders the rename, not the data behind it, nor the rename's own entry.

    Both syncs matter and they are different: without the first the file can
    come back empty after a power cut, without the second the rename itself can
    be lost and the old file reappear — carrying the old key.
    """
    synced: list[bool] = []
    real_fsync = os.fsync

    def record(descriptor: int) -> None:
        synced.append(os.fstat(descriptor).st_mode & 0o170000 == 0o040000)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record)
    settings.set_value(settings.LLM_CYCLE_BUDGET, "900")

    assert synced == [False, True], "expected one file fsync then one directory fsync"


def test_the_temp_file_is_made_in_the_same_directory_and_leaves_nothing_behind(store):
    """A rename across a mount boundary is ``EXDEV``, and the deployment bind-mounts /data."""
    settings.set_value(settings.LLM_CYCLE_BUDGET, "900")

    leftovers = [entry.name for entry in store.parent.iterdir() if entry.name.startswith(".")]
    assert leftovers == []
    assert sorted(entry.name for entry in store.parent.iterdir()) == [
        "settings.env",
        "settings.env.lock",
    ]


def test_a_write_creates_the_data_directory_rather_than_failing_on_the_lockfile(tmp_path):
    """A fresh install can store a setting before the graph exists.

    The lock is taken first, so a directory that does not exist yet failed on
    the *lockfile* with a bare ``FileNotFoundError`` before anything could
    create it.
    """
    settings.reset()
    fresh = tmp_path / "not-yet" / "deeper"
    settings.bind(fresh / "graph.db")

    settings.set_value(settings.LLM_CYCLE_BUDGET, "900")

    assert (fresh / settings.SETTINGS_FILENAME).read_text(encoding="utf-8").strip() == (
        f"{settings.LLM_CYCLE_BUDGET}=900"
    )


def test_two_threads_writing_different_keys_lose_nothing(store):
    """flock does not serialise threads of one process, and a threading lock
    does not serialise processes; the write path holds both."""
    errors: list[BaseException] = []

    def write(key: str, value: str) -> None:
        try:
            for _ in range(20):
                settings.set_value(key, value)
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [
        threading.Thread(target=write, args=(settings.LLM_CYCLE_BUDGET, "111")),
        threading.Thread(target=write, args=(settings.LLM_REQUEST_BUDGET, "222")),
        threading.Thread(target=write, args=(settings.LLM_MAX_OUTPUT_TOKENS, "333")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    stored = settings.stored_values()
    assert stored[settings.LLM_CYCLE_BUDGET] == "111"
    assert stored[settings.LLM_REQUEST_BUDGET] == "222"
    assert stored[settings.LLM_MAX_OUTPUT_TOKENS] == "333"


#: A child process that takes the settings lock and holds it until a file appears.
_HOLD_THE_LOCK = """
import fcntl, os, pathlib, sys, time
lock = pathlib.Path(sys.argv[1])
release = pathlib.Path(sys.argv[2])
descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(descriptor, fcntl.LOCK_EX)
pathlib.Path(sys.argv[3]).write_text("held")
while not release.exists():
    time.sleep(0.01)
os.close(descriptor)
"""


def test_a_write_waits_for_another_process_holding_the_lock(store, tmp_path):
    """The lock is the kernel's, so it crosses processes and dies with its holder.

    Deterministic in both directions: with the lock held the write *cannot*
    finish, and without an ``flock`` it would finish immediately — which is
    exactly the assertion below.
    """
    held, release = tmp_path / "held", tmp_path / "release"
    child = subprocess.Popen(
        [sys.executable, "-c", _HOLD_THE_LOCK, str(store) + ".lock", str(release), str(held)]
    )
    try:
        for _ in range(500):
            if held.exists():
                break
            threading.Event().wait(0.01)
        assert held.exists(), "the child never took the lock"

        done = threading.Event()

        def write() -> None:
            settings.set_value(settings.LLM_CYCLE_BUDGET, "900")
            done.set()

        writer = threading.Thread(target=write)
        writer.start()
        assert not done.wait(0.3), "the write did not wait for the lock the child holds"
        release.write_text("go")
        assert done.wait(5.0), "the write never completed after the lock was released"
        writer.join()
    finally:
        release.write_text("go")
        child.wait(timeout=5)

    assert settings.stored_values()[settings.LLM_CYCLE_BUDGET] == "900"


def test_a_file_that_appears_or_vanishes_is_a_change_and_not_a_silence(store):
    """A missing file is a *state*: keyed as "unchanged" it would never be re-read."""
    settings.set_value(settings.LLM_CYCLE_BUDGET, "900")
    assert settings.resolve(settings.LLM_CYCLE_BUDGET) == "900"
    before = settings.generation()

    store.unlink()

    assert settings.resolve(settings.LLM_CYCLE_BUDGET) == "0"
    assert settings.generation() != before


def test_an_in_memory_database_has_nowhere_to_keep_a_settings_file():
    """Deriving a directory from ``:memory:`` would put a 0600 file holding an API
    key wherever the process happened to start."""
    with pytest.raises(settings.SettingRefused, match="in-memory"):
        settings.settings_path(":memory:")

    settings.reset()
    assert settings.bind(":memory:") is None
    assert settings.bound_path() is None
    # And with nothing bound, resolution still answers from the environment and
    # the defaults rather than raising at the first read.
    assert settings.resolve(settings.LLM_CYCLE_BUDGET) == "0"


def test_the_path_is_taken_from_the_database_it_was_handed(tmp_path):
    """``nodum serve --db PATH`` does not set NODUM_DB; a module that read the
    environment for itself would read settings beside a different graph."""
    settings.reset()
    assert settings.bind(tmp_path / "elsewhere" / "graph.db") == (
        tmp_path / "elsewhere" / "settings.env"
    )


# ── What may not be stored ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    [settings.DB, settings.LLM_BASE_URL, settings.EMBED_CACHE, settings.PUBLIC_URL],
)
def test_the_environment_only_keys_refuse_a_write_and_say_why(store, key: str):
    """Each of the four is refused for its own reason, and the reason is the answer."""
    with pytest.raises(settings.SettingRefused) as refusal:
        settings.set_value(key, "anything")

    assert key in str(refusal.value)
    assert settings.SPECS[key].refusal is not None
    assert str(settings.SPECS[key].refusal) in str(refusal.value)


def test_a_key_the_environment_pins_refuses_the_write_instead_of_accepting_a_dead_one(
    store, monkeypatch
):
    """The CLI form of the refusal the browser will get: accepted-and-inert is
    the failure the whole validation posture exists to forbid."""
    monkeypatch.setenv(settings.LLM_MODEL, "deepseek-chat")

    with pytest.raises(settings.SettingRefused, match="never be used"):
        settings.set_value(settings.LLM_MODEL, "qwen3:8b")

    assert not store.exists()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        (settings.LLM_CONTEXT_TOKENS, "lots"),
        (settings.LLM_CONTEXT_TOKENS, "0"),
        (settings.LLM_CYCLE_SECONDS, "inf"),
        (settings.LLM_CYCLE_SECONDS, "nan"),
        (settings.LLM_CYCLE_SECONDS, "-1"),
        (settings.LLM_THINKING, "very"),
        (settings.CONSOLIDATE_AT, "3pm"),
        (settings.AUDIO_DOWNLOAD, "yes"),
        (settings.LLM_CYCLE_BUDGET, "-5"),
    ],
)
def test_a_value_the_runtime_would_discard_never_reaches_the_file(store, key: str, value: str):
    """The file never holds something that would be read back and dropped.

    Two of the codebase's readers swallow a bad value and fall back and one
    refuses outright, so a stored bad value would be *accepted, shown with
    settings.env provenance, and never applied* — through whichever door its
    key happens to use.
    """
    with pytest.raises(settings.SettingRefused, match=key):
        settings.set_value(key, value)

    assert not store.exists()


def test_an_unknown_key_is_refused_by_name(store):
    with pytest.raises(settings.SettingRefused, match="is not a nodum setting"):
        settings.set_value("NODUM_NOT_A_SETTING", "1")


def test_unsetting_a_key_that_was_never_set_changes_nothing_and_is_not_an_error(store):
    change = settings.unset_value(settings.LLM_CYCLE_BUDGET)

    assert (change.before, change.after, change.changed) == (None, None, False)


# ── Liveness: the three classes, driven through the store ────────────────────


def test_a_budget_written_now_funds_the_next_run_and_not_the_one_in_flight(store, fresh_db):
    """ "Live" for a budget means the *next* ``AgentRun``, and saying more would be a lie.

    One ``AgentRun`` spans a whole nightly cycle, so lowering the budget does
    not stop a cycle already spending — the kill switch is ``cycle-stop``, and
    a surface that implied otherwise would send a human to the wrong control.
    """
    llm.set_provider(None, reason="test default: no LLM provider")
    in_flight = agent.for_request(purpose="ask", principal=owner())
    assert in_flight.budget.tokens == agent.DEFAULT_REQUEST_BUDGET

    settings.set_value(settings.LLM_REQUEST_BUDGET, "42")

    assert in_flight.budget.tokens == agent.DEFAULT_REQUEST_BUDGET, "an in-flight run is not moved"
    assert agent.for_request(purpose="ask", principal=owner()).budget.tokens == 42


#: A child process that binds the same graph and stores a model in it.
_WRITE_A_MODEL = """
import sys
from nodum import settings
settings.bind(sys.argv[1])
settings.set_value(settings.LLM_MODEL, "qwen3:8b")
"""


def test_a_model_written_from_another_process_reaches_this_one_without_a_restart(store):
    """The claim that matters, made the only way it is worth making.

    A second interpreter binds the same file and writes a model; nothing in
    this process is reset, restarted or told about it — and the next
    ``get_provider`` resolves the new one, because the stamp on the file moved.
    """
    llm.reset_provider()
    assert llm.get_provider() is None

    subprocess.run(
        [sys.executable, "-c", _WRITE_A_MODEL, str(store.parent / "graph.db")],
        check=True,
        capture_output=True,
    )

    provider = llm.get_provider()
    assert provider is not None and provider.model_id == "qwen3:8b"
    assert llm.unavailable_reason() is None


def test_a_forced_provider_survives_a_settings_write(store):
    """The suite's own network guard is a pin, and a write must not discard it.

    ``conftest`` forces ``set_provider(None)`` on every test precisely so the
    suite never reaches a developer's local ollama or a paid API. Under a
    generation check that ignored the pin, the first settings write in any test
    would have re-resolved from the ambient environment.
    """
    llm.set_provider(None, reason="test default: no LLM provider")

    settings.set_value(settings.LLM_MODEL, "qwen3:8b")

    assert llm.get_provider() is None
    assert llm.unavailable_reason() == "test default: no LLM provider"
    # And the pin is not permanent: dropping it re-resolves from what is stored.
    llm.reset_provider()
    provider = llm.get_provider()
    assert provider is not None and provider.model_id == "qwen3:8b"


def test_a_settings_write_never_triggers_an_embedding_model_load(store, monkeypatch):
    """Constructing the embedding provider *loads a model*, so its invalidation is
    keyed on its own three values rather than on the settings file moving."""
    embeddings.reset_provider()
    loads: list[int] = []
    monkeypatch.setattr(
        embeddings, "_resolve_default", lambda: (loads.append(1), (None, "stub"))[1]
    )
    embeddings.get_provider()
    assert loads == [1]

    settings.set_value(settings.LLM_CYCLE_BUDGET, "900")
    embeddings.get_provider()

    assert loads == [1], "a budget write resolved the embedding provider again"


# ── Remediation strings name the layer in force ───────────────────────────────


def test_a_provider_refusal_names_the_layer_the_bad_value_came_from(store):
    """ "Set NODUM_LLM_THINKING" is useless advice to somebody who already set it.

    There are two layers now, so a message naming only the variable sends an
    operator to edit whichever one they think of first — and if that is the
    environment while the value is in the file, they change nothing and the
    refusal repeats. Only a hand-edit can produce this state (the write path
    validates), which is exactly why the message has to say where to look.
    """
    store.write_text(
        f"{settings.LLM_MODEL}=qwen3:8b\n{settings.LLM_THINKING}=very\n", encoding="utf-8"
    )
    llm.reset_provider()

    reason = llm.unavailable_reason()

    assert reason is not None
    assert settings.LLM_THINKING in reason
    assert settings.SETTINGS_FILENAME in reason


def test_an_unfunded_cycle_names_the_layer_its_budget_came_from(store, fresh_db):
    """The gardener's "CYCLE_BUDGET is 0" note, and where the 0 came from."""
    llm.set_provider(None, reason="test default: no LLM provider")
    assert agent.for_cycle(cycle_id="c1", principal=owner()).budget_source == "the built-in default"

    settings.set_value(settings.LLM_CYCLE_BUDGET, "900")

    assert agent.for_cycle(cycle_id="c1", principal=owner()).budget_source == (
        settings.SETTINGS_FILENAME
    )


# ── Secrets ───────────────────────────────────────────────────────────────────


def test_the_stored_api_key_never_reaches_a_surface(store, fresh_db):
    """The sweep: stdout, stderr, the event log, and the exception text.

    There was no never-print rule for this value before this project — the one
    the design cited turned out to be about the database path — so the
    invariant is built rather than inherited, and this is what holds it.
    """
    stored = runner.invoke(
        cli.app, ["config", "set", settings.LLM_API_KEY, SECRET, "--as", "owner"]
    )
    assert stored.exit_code == 0, stored.output + stored.stderr
    assert SECRET not in stored.stdout
    assert SECRET not in stored.stderr

    for command in (
        ["config", "list"],
        ["config", "get", settings.LLM_API_KEY],
        ["events", "--as", "owner"],
    ):
        result = runner.invoke(cli.app, command)
        assert result.exit_code == 0, result.output + result.stderr
        assert SECRET not in result.stdout, command
        assert SECRET not in result.stderr, command

    # It really is stored — the sweep would pass on a key that was dropped.
    assert store.read_text(encoding="utf-8").strip() == f"{settings.LLM_API_KEY}={SECRET}"
    assert json.loads(runner.invoke(cli.app, ["config", "get", settings.LLM_API_KEY]).stdout) == {
        "key": settings.LLM_API_KEY,
        "value": None,
        "set": True,
        "provenance": settings.FROM_FILE,
        "default": None,
        "kind": "secret",
        "secret": True,
        "writable": True,
        "refusal": None,
        "stored": True,
        # Free text: there is no invalid value for it, so there is no posture
        # to report. A wrong key is a 401 from the endpoint, not a refusal here.
        "on_invalid": None,
    }


def test_a_malformed_line_is_never_quoted_into_a_log_or_a_message(store, caplog):
    """The blocker: the likeliest hand-edit in this file carries the API key.

    ``export NODUM_LLM_API_KEY=sk-…`` is a habit carried over from a shell
    profile, and its space breaks the key shape — so the line that fails most
    often is exactly the one holding the credential. Quoting it published the
    key twice over: at ERROR into the container's unrotated log, and through
    the CLI's error boundary onto the operator's terminal.
    """
    store.write_text(f"export {settings.LLM_API_KEY}={SECRET}\n", encoding="utf-8")

    with caplog.at_level("DEBUG", logger="nodum.settings"):
        assert settings.resolve(settings.LLM_API_KEY) is None

    assert settings.unreadable_reason() is not None
    assert SECRET not in caplog.text
    assert SECRET not in (settings.unreadable_reason() or "")
    # And the write path's refusal, which carries the same text to stderr.
    with pytest.raises(settings.SettingsFileUnreadable) as refusal:
        settings.set_value(settings.LLM_CYCLE_BUDGET, "900")
    assert SECRET not in str(refusal.value)
    assert "line 1" in str(refusal.value)


def test_a_malformed_line_still_names_the_key_when_it_can_be_read_safely(store):
    """Redaction must not cost the reader the one thing they need to fix it.

    ``export KEY=value`` is the one shape where the key is recoverable without
    guessing, so it is named and its value is not. A line with no ``=`` names
    nothing: no part of it is known not to be a value.
    """
    store.write_text(f"export {settings.LLM_MODEL}=qwen3:8b\n", encoding="utf-8")
    settings.resolve(settings.LLM_MODEL)
    named = settings.unreadable_reason() or ""
    assert "line 1" in named
    assert settings.LLM_MODEL in named
    assert "qwen3:8b" not in named

    settings.reset()
    settings.bind(store.parent / "graph.db")
    store.write_text("NODUM_LLM_MODEL qwen3:8b\n", encoding="utf-8")
    settings.resolve(settings.LLM_MODEL)
    silent = settings.unreadable_reason() or ""
    assert "line 1" in silent
    assert "qwen3:8b" not in silent


def test_the_secret_survives_no_path_out_of_a_malformed_file_through_the_cli(store, fresh_db):
    """The sweep, over the malformed-file case rather than the well-formed one."""
    store.write_text(f"export {settings.LLM_API_KEY}={SECRET}\n", encoding="utf-8")

    for command in (
        ["config", "list"],
        ["config", "get", settings.LLM_API_KEY],
        ["config", "set", settings.LLM_CYCLE_BUDGET, "900", "--as", "owner"],
        ["config", "unset", settings.LLM_CYCLE_BUDGET, "--as", "owner"],
    ):
        result = runner.invoke(cli.app, command)
        assert SECRET not in result.stdout, command
        assert SECRET not in result.stderr, command


def test_a_refusal_about_a_secret_does_not_quote_the_secret(store):
    """The exception text is a surface too, and this one is raised on the value."""
    with pytest.raises(settings.SettingRefused) as refusal:
        settings.set_value(settings.LLM_API_KEY, f"{SECRET}\nNODUM_LLM_MODEL=stolen")

    assert SECRET not in str(refusal.value)


def test_the_event_payload_reduces_a_secret_to_whether_it_is_set(store):
    """The log is append-only and every projector rebuild reads it end to end."""
    change = settings.set_value(settings.LLM_API_KEY, SECRET)

    assert change.event_payload() == {
        "key": settings.LLM_API_KEY,
        "before": "unset",
        "after": "set",
    }
    assert settings.set_value(settings.LLM_CYCLE_BUDGET, "900").event_payload() == {
        "key": settings.LLM_CYCLE_BUDGET,
        "before": None,
        "after": "900",
    }


# ── The CLI surface ───────────────────────────────────────────────────────────


def test_config_list_reports_every_key_with_its_provenance(store, fresh_db):
    payload = _run_json("config", "list")

    assert payload["count"] == len(settings.KEYS)
    assert [row["key"] for row in payload["settings"]] == list(settings.KEYS)
    assert payload["path"] == str(store)
    assert payload["unreadable"] is None
    assert {row["provenance"] for row in payload["settings"]} <= {
        settings.FROM_ENVIRONMENT,
        settings.FROM_FILE,
        settings.FROM_DEFAULT,
        settings.FROM_UNSET,
    }


def test_config_set_is_logged_and_config_unset_takes_it_back(store, fresh_db):
    before = max((event.seq for event in service.list_events(owner(), limit=5000)), default=0)

    payload = _run_json("config", "set", settings.LLM_CYCLE_BUDGET, "900", "--as", "owner")
    assert payload == {
        "key": settings.LLM_CYCLE_BUDGET,
        "changed": True,
        "stored": True,
        "value": "900",
        "set": True,
        "provenance": settings.FROM_FILE,
    }

    events = [e for e in service.list_events(owner(), limit=5000) if e.seq > before]
    assert [event.op for event in events] == ["settings.set"]
    assert events[0].actor == "human:owner"
    assert events[0].payload == {
        "key": settings.LLM_CYCLE_BUDGET,
        "before": None,
        "after": "900",
    }

    cleared = _run_json("config", "unset", settings.LLM_CYCLE_BUDGET, "--as", "owner")
    assert (cleared["changed"], cleared["stored"], cleared["value"]) == (True, False, "0")
    # `list_events` answers newest first.
    assert [e.op for e in service.list_events(owner(), limit=5000) if e.seq > before] == [
        "settings.unset",
        "settings.set",
    ]


def test_a_settings_write_is_not_reversible_by_undo(store, fresh_db):
    """It is audit-only by construction: ``undo`` reverses graph events only, and
    a file outside the database is not something a transaction can put back."""
    _run_json("config", "set", settings.LLM_CYCLE_BUDGET, "900", "--as", "owner")

    result = runner.invoke(cli.app, ["undo", "--as", "owner"])

    assert result.exit_code == 1
    assert settings.resolve(settings.LLM_CYCLE_BUDGET) == "900"


def test_config_set_refuses_an_environment_only_key_with_the_reason(store, fresh_db):
    result = runner.invoke(
        cli.app, ["config", "set", settings.LLM_BASE_URL, "http://x/v1", "--as", "owner"]
    )

    assert result.exit_code == 1
    assert result.stdout.strip() == ""
    assert settings.LLM_BASE_URL in result.stderr


def test_config_get_refuses_a_name_that_is_not_a_setting(store, fresh_db):
    result = runner.invoke(cli.app, ["config", "get", "NODUM_NOPE"])

    assert result.exit_code == 1
    assert result.stdout.strip() == ""


def test_backup_carries_the_settings_file_beside_the_database(store, fresh_db, tmp_path):
    """A by-the-book restore that reverted every setting including the key was
    the failure this closes, and three published statements depended on it."""
    settings.set_value(settings.LLM_API_KEY, SECRET)
    destination = tmp_path / "backups" / "graph.db"

    payload = _run_json("backup", str(destination))

    assert payload["settings"] == f"{destination}.settings.env"
    copied = Path(payload["settings"])
    assert copied.read_text(encoding="utf-8") == store.read_text(encoding="utf-8")
    assert copied.stat().st_mode & 0o777 == 0o600


def test_backup_refuses_an_occupied_settings_destination_before_copying_anything(
    store, fresh_db, tmp_path
):
    """A backup either runs or does not.

    The refusal used to be raised *after* ``VACUUM INTO``, so an occupied
    ``<dest>.settings.env`` left the database copy written, printed no JSON and
    exited 1 — a backup that half happened and reported nothing about the half
    that did.
    """
    settings.set_value(settings.LLM_API_KEY, SECRET)
    destination = tmp_path / "backups" / "graph.db"
    destination.parent.mkdir(parents=True)
    occupied = destination.with_name(f"{destination.name}.{settings.SETTINGS_FILENAME}")
    occupied.write_text("someone else's file\n", encoding="utf-8")

    result = runner.invoke(cli.app, ["backup", str(destination)])

    assert result.exit_code == 1
    assert result.stdout.strip() == ""
    assert not destination.exists(), "the database was copied before the refusal"
    assert occupied.read_text(encoding="utf-8") == "someone else's file\n"


def test_config_set_reports_the_write_before_it_logs_it(store, fresh_db, monkeypatch):
    """The write already happened, so a failed event must not swallow the answer.

    ``_emit_batch``'s rule, for the same reason: the envelope goes to stdout
    before the exit code is decided. A database error while logging is one line
    on stderr and exit 1, over a stdout that still says truthfully what the file
    now holds.
    """

    def refuse(*args: object, **kwargs: object) -> int:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(service, "record_settings_event", refuse)

    result = runner.invoke(
        cli.app, ["config", "set", settings.LLM_CYCLE_BUDGET, "900", "--as", "owner"]
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["value"] == "900"
    assert settings.resolve(settings.LLM_CYCLE_BUDGET) == "900"
    assert result.stderr.strip() != ""


def test_a_backup_of_a_graph_with_no_settings_file_says_so_rather_than_inventing_one(
    store, fresh_db, tmp_path
):
    payload = _run_json("backup", str(tmp_path / "backups" / "graph.db"))

    assert payload["settings"] is None
