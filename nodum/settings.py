"""Configuration written beside the graph — the ``settings.env`` store and the ladder over it.

Until this module there was one place configuration came from, and it was the
process environment: to change a model, a budget or the nightly schedule you
edited a compose file or a shell profile and restarted the server. That is the
right answer for a deployment secret and the wrong one for a knob an operator
turns, so a second layer lives here — one file, ``settings.env``, in the same
directory as the database, read on every resolution and written through a
single validated door.

**The ladder is default < settings.env < environment**, and *empty is not set
at any layer*. The environment winning is deliberate: the deployed container
renders six of its variables as ``${VAR:-}`` pass-throughs and one as a bare
interpolation, so ``"NAME" in os.environ`` is true for ten of nineteen names
there and **seven of those are empty** — a precedence keyed on *presence* would
pin every one of them to an empty string and make the file unreachable.
Presence is not a signal here; a non-empty value is.

**The path is threaded in, never re-derived.** :func:`bind` takes the database
path the *caller* resolved, because ``nodum serve --db PATH`` does not set
``NODUM_DB`` — only the global ``nodum --db`` does. A module that read the
environment for itself would serve one graph and read settings beside another,
and the frontend's end-to-end fixture spawns exactly that shape, which would
have put a 0600 file carrying an API key into the developer's real data
directory.

**One writer at a time, and a write that survives a power cut.** A write takes
the in-process ``write_lock`` *and* an ``flock`` on a sibling lockfile — flock
alone does not serialise two threads of one process, and a threading lock does
not serialise two processes — and holds both across the whole read → merge →
render → replace span, with the read coming from the disk rather than from the
cache. The new bytes are written to an ``O_EXCL`` temp file in the *same*
directory (a rename across a mount boundary is ``EXDEV``, and the deployment
bind-mounts ``/data``), created 0600 rather than chmod-ed afterwards, then
``fsync``-ed, ``os.replace``-d, and the directory ``fsync``-ed after it. A
reader therefore sees the old inode or the new one and never a half-written
file.

**A reader never waits on a writer.** The writer's lock and the cache's lock
are different locks, and the cache's is held only while the view is swapped —
never across the ``flock`` wait, the ``fsync``s or the ``os.replace``. Sharing
one lock made every ``resolve`` wait on whatever process happened to hold the
file, measured at 1.8 s, and the readers include the scheduler's slice on the
event loop and :func:`nodum.llm.resolution`, which asks while holding its own
lock — so one stuck writer stalled the loop and every path to the model behind
it.

**The cache is keyed on the file's identity, not on its clock.** The stamp is
``(st_dev, st_ino, st_mtime_ns, st_size)`` taken by ``fstat`` on the same
descriptor, and compared with ``!=`` rather than ``>`` — ``os.replace`` gives a
new inode whose mtime can be *older* than the one it replaced (a file restored
from a backup, a coarse-granularity mount), and a ``>`` comparison would call
that "unchanged" forever. A file that has gone missing is an empty store and a
generation bump, not "unchanged". The comparison happens **before** the read,
so an unchanged file costs one ``open`` and one ``fstat`` and nothing else: no
``read``, no bytes, no parse. The generation the comparison feeds is
process-wide rather than per file, so rebinding from one graph to another
cannot hand two different readings the same number.

**Secrets are structural.** :data:`SECRET_KEYS` names the values this module
will not serialise, and the only way to build an event payload for a change is
:meth:`Change.event_payload`, which reduces them to ``"set"`` / ``"unset"``.
Nothing here returns an API key to a surface: :func:`get_setting` reports
whether one is set and never what it is.

**A file it cannot parse is reported loudly and stepped around.** Resolution
continues on the environment and the defaults — the posture the scheduler
already takes with an unparseable ``NODUM_CONSOLIDATE_AT`` — and the provenance
of every affected key becomes :data:`FROM_UNREADABLE`, so a surface can say
*why* a value the operator set is not in force. A write against an unreadable
file is refused rather than rewriting it: a file this cannot parse is a file
whose other lines it cannot promise to preserve.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from secrets import token_hex
from types import MappingProxyType

from nodum.models import SettingChangeOut, SettingOut, SettingsOut

logger = logging.getLogger(__name__)

#: The settings file's name; it lives in the database's own directory, so a
#: graph and its configuration move, are backed up, and are restored together.
SETTINGS_FILENAME = "settings.env"

#: Suffix of the lockfile guarding the read-modify-write span. A separate file
#: rather than a lock on ``settings.env`` itself: the file being replaced is a
#: different inode from the one a writer opened, so a lock held on it would
#: guard nothing after the first write.
LOCK_SUFFIX = ".lock"


# ── Failures ─────────────────────────────────────────────────────────────────


class SettingRefused(ValueError):
    """A setting could not be written: unknown, not storable, pinned, or invalid.

    A ``ValueError`` so both surfaces' existing error boundaries carry it as
    the one readable line they promise, rather than as a traceback.
    """


class SettingsFileUnreadable(ValueError):
    """``settings.env`` exists and could not be parsed.

    Raised only by the *write* path. Resolution never raises it — a file it
    cannot read is logged and stepped around, because a server that will not
    answer because of a stray character in an optional file is worse than one
    that says what it ignored.
    """


# ── The key registry ─────────────────────────────────────────────────────────

DB = "NODUM_DB"
PUBLIC_URL = "NODUM_PUBLIC_URL"
CONSOLIDATE_AT = "NODUM_CONSOLIDATE_AT"
LLM_MODEL = "NODUM_LLM_MODEL"
LLM_BASE_URL = "NODUM_LLM_BASE_URL"
LLM_API_KEY = "NODUM_LLM_API_KEY"
LLM_CONTEXT_TOKENS = "NODUM_LLM_CONTEXT_TOKENS"
LLM_THINKING = "NODUM_LLM_THINKING"
LLM_CYCLE_BUDGET = "NODUM_LLM_CYCLE_BUDGET"
LLM_CYCLE_SECONDS = "NODUM_LLM_CYCLE_SECONDS"
LLM_REQUEST_BUDGET = "NODUM_LLM_REQUEST_BUDGET"
LLM_REQUEST_SECONDS = "NODUM_LLM_REQUEST_SECONDS"
LLM_CALL_TIMEOUT = "NODUM_LLM_CALL_TIMEOUT"
LLM_MAX_OUTPUT_TOKENS = "NODUM_LLM_MAX_OUTPUT_TOKENS"
EMBED_CACHE = "NODUM_EMBED_CACHE"
AUDIO_MODEL = "NODUM_AUDIO_MODEL"
AUDIO_DOWNLOAD = "NODUM_AUDIO_DOWNLOAD"

#: The values this module will not serialise — not to a surface, not into an
#: event payload, not into an exception message. A frozenset rather than a
#: naming convention because the rule has to survive a key whose name does not
#: contain "key" or "secret".
SECRET_KEYS = frozenset({LLM_API_KEY})

#: Where a value in force came from. ``FROM_UNSET`` means nothing set it and it
#: has no default; ``FROM_UNREADABLE`` means the environment did not set it and
#: the file could not be read, so the default is in force *and this cannot say
#: the file was silent*.
FROM_ENVIRONMENT = "environment"
FROM_FILE = "settings.env"
FROM_DEFAULT = "default"
FROM_UNSET = "unset"
FROM_UNREADABLE = "file-unreadable"

#: What the *runtime* does with a value it cannot use. The postures already in
#: the codebase, named per key so a surface and the docs can state which one
#: applies: ``fall-back`` is :mod:`nodum.agent`'s rule (a bad number becomes the
#: default, because the worst case is less work), ``refuse`` is :mod:`nodum.llm`'s
#: (a bad reasoning level or window means no provider at all, with a reason),
#: and ``off`` is the scheduler's (announced and ignored). The write path
#: validates ahead of all three, so only a hand-edit can reach them.
#:
#: A key whose validator is :func:`_text` reports **``None``**, not one of
#: these. There is no such thing as an invalid value for it — any non-empty
#: string is accepted here *and* by the runtime, and what happens to a wrong
#: one happens later and elsewhere: a model name nothing serves is an HTTP
#: error on the first call, a Whisper size nothing ships raises inside
#: ``WhisperModel``. Reporting ``refuse`` or ``fall-back`` there described a
#: check that does not exist.
ON_INVALID_FALLBACK = "fall-back"
ON_INVALID_REFUSE = "refuse"
ON_INVALID_OFF = "off"

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_KEY_SHAPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_daily_time(value: str | None) -> time | None:
    """Parse ``HH:MM`` (or ``HH:MM:SS``) into a local wall-clock time.

    Lives here rather than in :mod:`nodum.scheduler` so the validator that
    guards the *write* and the parser that reads the value at *run* time are one
    function: a schedule this accepts and the scheduler then rejects would be
    the accepted-but-inert edit the whole validation table exists to forbid.

    Args:
        value: The configured value; ``None`` or blank means "not configured".

    Returns:
        The time of day the cycle should run at, or ``None`` when the scheduler
        is off.

    Raises:
        ValueError: If the value is not a 24-hour ``HH:MM`` time. This refuses
            rather than falling back to "off", so the caller gets to decide what
            a typo means; ``nodum.http_api`` announces it and starts without a
            schedule, because a server that will not boot over a stray character
            in an optional setting is worse than one that says what it ignored.

            A UTC offset is refused too. ``time.fromisoformat`` accepts
            ``03:30+05:00`` happily, and the scheduler then reads the result as
            a *local wall clock* — which is the one thing an offset says it is
            not — so the cycle would run at 03:30 local while its configuration
            asked for 03:30 somewhere else.
    """
    if value is None or not value.strip():
        return None
    try:
        parsed = time.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"{CONSOLIDATE_AT} must be a 24-hour HH:MM time, got {value!r}") from exc
    if parsed.tzinfo is not None:
        raise ValueError(
            f"{CONSOLIDATE_AT} is a local wall-clock time and takes no UTC offset, got {value!r}"
        )
    return parsed


def _whole_number(minimum: int) -> Callable[[str, str], str]:
    """A validator accepting a whole number of at least ``minimum``."""

    def validate(name: str, value: str) -> str:
        try:
            parsed = int(value)
        except ValueError:
            raise SettingRefused(f"{name} must be a whole number, got {value!r}") from None
        if parsed < minimum:
            raise SettingRefused(f"{name} must be at least {minimum}, got {parsed}")
        return str(parsed)

    return validate


def _seconds(name: str, value: str) -> str:
    """A validator accepting a positive, finite number of seconds.

    Finite because both serialisers this number reaches are ``json.dumps``
    without ``allow_nan``, which writes a bare ``Infinity`` token that is not
    JSON; and ``nan`` is worse in a quieter way, since every comparison against
    it is false and the wall-clock ceiling stops existing without saying so.
    """
    try:
        parsed = float(value)
    except ValueError:
        raise SettingRefused(f"{name} must be a number of seconds, got {value!r}") from None
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        raise SettingRefused(f"{name} must be a finite number of seconds, got {value!r}")
    if parsed <= 0:
        raise SettingRefused(f"{name} must be greater than zero, got {value!r}")
    return value


def _one_of(allowed: tuple[str, ...]) -> Callable[[str, str], str]:
    """A validator accepting one of ``allowed``, case-insensitively."""

    def validate(name: str, value: str) -> str:
        folded = value.casefold()
        if folded not in allowed:
            raise SettingRefused(f"{name} must be one of {', '.join(allowed)}, got {value!r}")
        return folded

    return validate


def _daily_time(name: str, value: str) -> str:
    """A validator accepting a 24-hour ``HH:MM`` local wall-clock time."""
    try:
        parse_daily_time(value)
    except ValueError as exc:
        raise SettingRefused(str(exc)) from exc
    return value


def _text(name: str, value: str) -> str:
    """A validator accepting any non-empty text (emptiness is refused upstream)."""
    return value


def _gate(name: str, value: str) -> str:
    """A validator for the ``"1"``-gates: exactly ``1`` (on) or ``0`` (off).

    The readers test ``== "1"``, so every other spelling is off — and a setting
    whose ``true`` and ``yes`` mean *off* is a setting that lies. Two values,
    both explicit.
    """
    if value not in ("0", "1"):
        raise SettingRefused(f"{name} must be '1' (on) or '0' (off), got {value!r}")
    return value


@dataclass(frozen=True)
class Setting:
    """One configurable name: what it means, what it accepts, and who may set it.

    ``writable`` false means the file layer is not consulted for this name at
    all — :func:`resolve` reads the environment and the default alone — and
    ``refusal`` says why, in the sentence a surface hands the operator.
    """

    name: str
    kind: str
    default: str | None
    summary: str
    validate: Callable[[str, str], str] = _text
    secret: bool = False
    writable: bool = True
    refusal: str | None = None
    on_invalid: str | None = ON_INVALID_FALLBACK

    def __post_init__(self) -> None:
        """Refuse a posture on a key that has no check to have a posture about.

        A registry row is what ``nodum config list`` publishes, so a field that
        can be filled in wrongly is a field that will be — and an operator
        reading ``refuse`` against a key nothing checks is being told a check
        exists.
        """
        if self.validate is _text and self.on_invalid is not None:
            raise ValueError(
                f"{self.name}: a key validated as free text has no invalid value, "
                f"so on_invalid must be None"
            )
        if self.validate is not _text and self.on_invalid is None:
            raise ValueError(f"{self.name}: a validated key must say what a bad value does")


#: Why the four environment-only names cannot be written here. Each is read
#: before or outside the window in which a settings file could matter, or it
#: decides where something on the server's own disk is read from — which is not
#: a decision a remote surface gets to make.
_ENV_ONLY_DB = (
    "the database path is read before the graph — and therefore before its "
    "settings file — is open; set it with --db or in the environment"
)
_ENV_ONLY_BASE_URL = (
    "the endpoint an API key may travel to is a deployment decision, not a "
    "stored one; set it in the environment"
)
_ENV_ONLY_EMBED_CACHE = "this is a path on the server's own disk; set it in the environment"
_ENV_ONLY_PUBLIC_URL = (
    "every capability URL is minted from it, so a stored value would redirect "
    "them; set it in the environment"
)

_SPECS: tuple[Setting, ...] = (
    Setting(
        name=DB,
        kind="path",
        default="~/.local/share/nodum/nodum.db",
        summary="The graph database path.",
        writable=False,
        refusal=_ENV_ONLY_DB,
        on_invalid=None,
    ),
    Setting(
        name=PUBLIC_URL,
        kind="url",
        default="http://127.0.0.1:8600",
        summary="The base URL minted capability URLs are built on.",
        writable=False,
        refusal=_ENV_ONLY_PUBLIC_URL,
        on_invalid=None,
    ),
    Setting(
        name=CONSOLIDATE_AT,
        kind="time",
        default=None,
        summary="Local wall-clock time the nightly consolidation cycle runs at (HH:MM).",
        validate=_daily_time,
        on_invalid=ON_INVALID_OFF,
    ),
    Setting(
        name=LLM_MODEL,
        kind="string",
        default=None,
        summary="The model name; unset means no provider and no smart features.",
        on_invalid=None,
    ),
    Setting(
        name=LLM_BASE_URL,
        kind="url",
        default="http://localhost:11434/v1",
        summary="OpenAI-compatible base URL; a shipped profile may supply it.",
        writable=False,
        refusal=_ENV_ONLY_BASE_URL,
        on_invalid=None,
    ),
    Setting(
        name=LLM_API_KEY,
        kind="secret",
        default=None,
        summary="Bearer token, sent only to an endpoint somebody named.",
        secret=True,
        on_invalid=None,
    ),
    Setting(
        name=LLM_CONTEXT_TOKENS,
        kind="int",
        default="4096",
        summary="The window the endpoint will serve; a shipped profile may raise it.",
        validate=_whole_number(1),
        on_invalid=ON_INVALID_REFUSE,
    ),
    Setting(
        name=LLM_THINKING,
        kind="enum",
        default="high",
        summary="The reasoning level: none, low, medium or high.",
        validate=_one_of(("none", "low", "medium", "high")),
        on_invalid=ON_INVALID_REFUSE,
    ),
    Setting(
        name=LLM_CYCLE_BUDGET,
        kind="int",
        default="0",
        summary="Tokens one consolidation cycle's LLM jobs may spend; 0 is off.",
        validate=_whole_number(0),
    ),
    Setting(
        name=LLM_CYCLE_SECONDS,
        kind="float",
        default="1800",
        summary="Wall-clock ceiling for one consolidation cycle's LLM jobs.",
        validate=_seconds,
    ),
    Setting(
        name=LLM_REQUEST_BUDGET,
        kind="int",
        default="8000",
        summary="Tokens one human-initiated request may spend.",
        validate=_whole_number(0),
    ),
    Setting(
        name=LLM_REQUEST_SECONDS,
        kind="float",
        default="180",
        summary="Wall-clock ceiling for one human-initiated request.",
        validate=_seconds,
    ),
    Setting(
        name=LLM_CALL_TIMEOUT,
        kind="float",
        default="120",
        summary="Per-call wall-clock ceiling handed to the provider.",
        validate=_seconds,
    ),
    Setting(
        name=LLM_MAX_OUTPUT_TOKENS,
        kind="int",
        default="4096",
        summary="Per-call output ceiling.",
        validate=_whole_number(1),
    ),
    Setting(
        name=EMBED_CACHE,
        kind="path",
        default="~/.local/share/nodum/models",
        summary="Where embedding model files are cached.",
        writable=False,
        refusal=_ENV_ONLY_EMBED_CACHE,
        on_invalid=None,
    ),
    Setting(
        name=AUDIO_MODEL,
        kind="string",
        default="base",
        summary="The Whisper model size used for audio transcription.",
        on_invalid=None,
    ),
    Setting(
        name=AUDIO_DOWNLOAD,
        kind="gate",
        default=None,
        summary="'1' allows the one-time transcription-model download.",
        validate=_gate,
    ),
)

#: The registry, in the order surfaces list it.
SPECS: Mapping[str, Setting] = MappingProxyType({spec.name: spec for spec in _SPECS})

#: Every name this module knows, in registry order.
KEYS: tuple[str, ...] = tuple(SPECS)


def specification(name: str) -> Setting:
    """Return one key's registry entry.

    Args:
        name: The setting's name.

    Returns:
        Its :class:`Setting`.

    Raises:
        SettingRefused: If nothing by that name is configurable.
    """
    spec = SPECS.get(name)
    if spec is None:
        raise SettingRefused(f"{name!r} is not a nodum setting")
    return spec


# ── The store: one per resolved path ─────────────────────────────────────────

_EMPTY: Mapping[str, str] = MappingProxyType({})

#: Cache stamps that are not a real ``fstat`` tuple, and cannot collide with
#: one. ``_NEVER_READ`` is the initial state, so the first resolution always
#: reads; ``_MISSING`` is a file that is not there, which is a *state* and not
#: an absence of one — a file that appears must bump the generation.
_NEVER_READ = ("never-read",)
_MISSING = ("missing",)


class _Store:
    """The cached view of one ``settings.env``, and the two locks that guard it.

    **Two, not one, and the split is the whole point.** ``write_lock`` is held
    across the read → merge → render → replace span, which includes waiting on
    another *process*'s ``flock`` and two ``fsync``s. ``cache_lock`` is held
    only while the cached view is swapped, which is a handful of attribute
    assignments. A single lock covering both made every *reader* wait on a
    foreign process's writer: measured at 1.8 s for one ``resolve`` while a
    child process held the lock — and the readers include the scheduler slice
    on the event loop and :func:`nodum.llm.resolution`, which holds its own
    lock while it asks. One stuck writer would have stalled the loop and every
    path to the model behind it.

    They are always taken in this order — ``write_lock`` then ``cache_lock`` —
    and never the reverse, so the pair cannot deadlock.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_name(path.name + LOCK_SUFFIX)
        #: Serialises writers in this process, across the whole write span.
        self.write_lock = threading.Lock()
        #: Guards the cached view itself. Short-lived by construction.
        self.cache_lock = threading.Lock()
        self.stamp: tuple[object, ...] = _NEVER_READ
        self.values: Mapping[str, str] = _EMPTY
        self.unknown: tuple[str, ...] = ()
        self.unreadable: str | None = None
        self.generation = _next_generation()


#: The generation counter is **process-wide**, not per store.
#:
#: Per store it started at 0 for each one, so a process that rebound from one
#: graph to another produced two stores whose counters both reached 1 — and a
#: cache comparing a bare integer (``nodum.llm``'s does) saw "unchanged" and
#: went on serving the first graph's provider while every other surface had
#: already followed the rebind. Measured. One counter for the process means a
#: generation is unique to the reading that produced it, whichever file that
#: was.
_generation_lock = threading.Lock()
_generation = 0


def _next_generation() -> int:
    """Take the next process-wide generation number."""
    global _generation
    with _generation_lock:
        _generation += 1
        return _generation


_registry_lock = threading.Lock()
_stores: dict[Path, _Store] = {}
_active_path: Path | None = None


def settings_path(db_path: str | Path) -> Path:
    """The settings file that belongs beside a graph database.

    Args:
        db_path: The database path the caller resolved.

    Returns:
        ``<database directory>/settings.env``.

    Raises:
        SettingRefused: For ``:memory:``, which has no directory — deriving one
            would put a 0600 file carrying an API key in whatever directory the
            process happened to start in.
    """
    text = str(db_path)
    if text == ":memory:":
        raise SettingRefused("an in-memory database has no directory to keep settings.env beside")
    return Path(text).expanduser().parent / SETTINGS_FILENAME


def bind(db_path: str | Path) -> Path | None:
    """Point this process's settings store at the file beside ``db_path``.

    Every entry point that resolves a database path calls this with the path it
    resolved — the CLI callback, ``nodum serve``, and :func:`nodum.http_api.create_app`
    — rather than letting this module read the environment for itself. That is
    the whole of the fix for ``nodum serve --db PATH``, which does not set
    ``NODUM_DB`` and would otherwise serve one graph while reading settings
    beside another.

    Args:
        db_path: The database path this process is working against.

    Returns:
        The bound settings path, or ``None`` for an in-memory database (which
        has no directory; the file layer is simply off, and resolution falls
        back to the environment and the defaults).
    """
    global _active_path
    try:
        path = settings_path(db_path)
    except SettingRefused:
        logger.debug(
            "%s has no directory for %s; the settings file layer is off",
            db_path,
            SETTINGS_FILENAME,
        )
        _active_path = None
        return None
    _active_path = path
    return path


def bound_path() -> Path | None:
    """The settings file this process is reading and writing, or ``None``."""
    return _active_path


def reset() -> None:
    """Unbind and drop every cached store — the test seam.

    Cache state is keyed per path, so a test that binds a fresh temp directory
    each time is already isolated; this exists for the test that must prove a
    *cold* read, and for the autouse fixture that leaves no state behind.
    """
    global _active_path
    with _registry_lock:
        _stores.clear()
    _active_path = None


def _store() -> _Store | None:
    """The active store, or ``None`` when nothing is bound."""
    path = _active_path
    if path is None:
        return None
    with _registry_lock:
        store = _stores.get(path)
        if store is None:
            store = _Store(path)
            _stores[path] = store
        return store


# ── The dialect ──────────────────────────────────────────────────────────────


def _describe_bad_line(number: int, line: str) -> str:
    """Name a malformed line **without quoting it**.

    The line is the one thing in this file that must never reach a message.
    Quoting it published the value of whatever key it carried to two places at
    once: :func:`_refresh` logs the refusal at ERROR, into an unrotated
    container log, and :func:`_write` raises it through the CLI's error
    boundary onto the operator's terminal. The likeliest malformed line in this
    file is ``export NODUM_LLM_API_KEY=sk-…`` — a habit carried over from a
    shell profile, and one whose space breaks :data:`_KEY_SHAPE` — so the shape
    that fails most often is exactly the one carrying the secret.

    What a reader needs to fix the file is *where* and, when it can be
    recovered safely, *which key*. Everything from the first ``=`` onwards is
    dropped unread, and what is left is named only when the **whole** of it is
    a key — optionally behind the one word that puts it there,
    ``export``. Naming the last token of the prefix instead would be more
    generous and wrong: a pasted ``Bearer sk_abc=…`` would have had ``sk_abc``
    named. A line with no ``=`` at all names nothing, because nothing in it is
    known not to be a value.

    Args:
        number: The 1-based line number.
        line: The offending line. Never echoed.

    Returns:
        A message naming the line, and the key when one can be read safely.
    """
    where = f"{SETTINGS_FILENAME} line {number}"
    prefix, separator, _value = line.partition("=")
    candidate = prefix.strip().removeprefix("export ").strip()
    if separator and _KEY_SHAPE.match(candidate):
        return f"{where} is not KEY=value (the key reads {candidate!r}; its value is not quoted)"
    return f"{where} is not KEY=value (the line is not quoted here: it may carry a secret)"


def _parse(text: str) -> tuple[dict[str, str], tuple[str, ...]]:
    """Parse ``settings.env`` into its values and the names this build does not know.

    The dialect is deliberately small: ``KEY=value`` one per line, LF, ``#``
    comments and blank lines ignored, no quoting and no escapes. A value is
    taken as written and stripped of surrounding whitespace, and an empty one
    is *not set* — the same rule the environment layer keeps.

    Args:
        text: The file's contents.

    Returns:
        The parsed values, and the keys that parsed cleanly but name nothing
        this build configures (preserved on every rewrite, never dropped).

    Raises:
        SettingsFileUnreadable: On a line that is not a comment, blank, or
            ``KEY=value``. A file whose shape is not understood is reported
            whole rather than half-applied — but the *line* is never quoted;
            see :func:`_describe_bad_line`.
    """
    values: dict[str, str] = {}
    unknown: list[str] = []
    seen_unknown: set[str] = set()
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, raw = line.partition("=")
        key = key.strip()
        if not separator or not _KEY_SHAPE.match(key):
            raise SettingsFileUnreadable(_describe_bad_line(number, line))
        value = raw.strip()
        if key not in SPECS:
            # Reported once however many times the file repeats it: the message
            # is a list of names, and a name said twice reads as two problems.
            if key not in seen_unknown:
                seen_unknown.add(key)
                unknown.append(key)
            continue
        if value:
            values[key] = value
        else:
            values.pop(key, None)
    return values, tuple(unknown)


def _render(text: str, name: str, value: str | None) -> str:
    """Rewrite ``text`` with ``name`` set to ``value``, or removed when it is ``None``.

    Comments, blank lines, layout and every key this build does not know are
    kept exactly as they were: this file is the operator's, and a rewrite that
    tidied it would lose whatever they put there for the next reader — or for
    the next version of nodum.

    Args:
        text: The file's current contents.
        name: The key to set or remove.
        value: Its new value, or ``None`` to remove it.

    Returns:
        The new contents, ending in a newline unless it is empty.
    """
    lines: list[str] = []
    written = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            key, separator, _ = line.partition("=")
            if separator and key.strip() == name:
                # The first occurrence carries the new value and any later
                # duplicate goes: two lines for one key is a file where the
                # last one silently wins, which is not a shape to preserve.
                if not written and value is not None:
                    lines.append(f"{name}={value}")
                    written = True
                continue
        lines.append(line)
    if value is not None and not written:
        lines.append(f"{name}={value}")
    return "".join(f"{line}\n" for line in lines)


# ── Reading ──────────────────────────────────────────────────────────────────


def _read_if_changed(
    path: Path, known: tuple[object, ...]
) -> tuple[tuple[object, ...], bytes | None]:
    """Stamp a file, and read it **only** when the stamp is not ``known``.

    ``os.stat`` before or after an ``open`` describes a file that may not be the
    one that was read. Stamping the open descriptor keeps the bytes and the
    stamp the same inode by construction — and comparing *before* reading is
    what makes the unchanged path actually cheap: one ``open`` and one
    ``fstat``, no ``read`` and no bytes. The first cut fstat-ed and then read
    the whole file every time and compared afterwards, which is 2 reads and the
    file's contents per resolution, against a docstring promising one ``stat``.

    Args:
        path: The file to read.
        known: The stamp the caller already has, or a sentinel.

    Returns:
        The file's identity stamp, and its contents — or ``None`` for the
        contents when the stamp is unchanged and nothing was read.
    """
    descriptor = os.open(path, os.O_RDONLY)
    try:
        status = os.fstat(descriptor)
        stamp = (status.st_dev, status.st_ino, status.st_mtime_ns, status.st_size)
        if stamp == known:
            return stamp, None
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 65536):
            chunks.append(chunk)
        return stamp, b"".join(chunks)
    finally:
        os.close(descriptor)


def _publish(
    store: _Store,
    stamp: tuple[object, ...],
    values: Mapping[str, str],
    unknown: tuple[str, ...],
    unreadable: str | None,
) -> bool:
    """Swap the cached view under ``cache_lock``, unless somebody got there first.

    Held for a handful of assignments and nothing else — emphatically **not**
    for the read, the parse, or any part of a write. That separation is what
    keeps a reader from waiting on a foreign process's ``flock``.

    Args:
        store: The store to publish into.
        stamp: The identity of the file this view was read from.
        values: The parsed values.
        unknown: Keys this build does not configure.
        unreadable: Why the file could not be parsed, or ``None``.

    Returns:
        Whether this call published. ``False`` means another thread had already
        published the same reading, so the generation is not bumped twice for
        one change.
    """
    with store.cache_lock:
        if store.stamp == stamp:
            return False
        store.stamp = stamp
        store.values = values
        store.unknown = unknown
        store.unreadable = unreadable
        store.generation = _next_generation()
        return True


def _refresh(store: _Store) -> None:
    """Reload the cached view if the file's identity changed.

    Takes ``cache_lock`` only to swap the result in; the ``open``, the
    ``fstat``, the ``read`` and the parse all happen outside every lock. Two
    threads refreshing at once therefore do the work twice and publish once,
    which is the trade this makes deliberately: duplicated reads are cheap and
    a blocked reader is not.
    """
    known = store.stamp
    try:
        stamp, raw = _read_if_changed(store.path, known)
    except FileNotFoundError:
        if _publish(store, _MISSING, _EMPTY, (), None):
            logger.debug("%s does not exist; the environment and the defaults decide", store.path)
        return
    except OSError as exc:
        if _publish(store, ("unreadable", str(exc)), _EMPTY, (), str(exc)):
            logger.error(
                "%s could not be read (%s); continuing on the environment and the defaults",
                store.path,
                exc,
            )
        return
    if raw is None:
        return
    try:
        values, unknown = _parse(raw.decode("utf-8"))
    except (UnicodeDecodeError, SettingsFileUnreadable) as exc:
        if _publish(store, stamp, _EMPTY, (), str(exc)):
            logger.error(
                "%s could not be parsed (%s); continuing on the environment and the defaults",
                store.path,
                exc,
            )
        return
    if _publish(store, stamp, MappingProxyType(values), unknown, None) and unknown:
        logger.info(
            "%s carries %d key(s) this build does not configure: %s (kept as they are)",
            store.path,
            len(unknown),
            ", ".join(unknown),
        )


def _current(store: _Store | None) -> tuple[Mapping[str, str], str | None, int]:
    """The file layer as it stands: its values, why it could not be read, its generation.

    All three come out of one hold of ``cache_lock``, generation read first.
    Read outside it, left to right, a refresh landing between the loads gave
    the *old* values stamped with the *new* generation — the exact inverse of
    what :class:`Snapshot` promises, and a cache holding that pair believes it
    is current forever.
    """
    if store is None:
        return _EMPTY, None, 0
    _refresh(store)
    with store.cache_lock:
        generation = store.generation
        return store.values, store.unreadable, generation


def _environment_value(name: str) -> str | None:
    """The environment's answer for ``name``, where empty is not an answer."""
    raw = (os.environ.get(name) or "").strip()
    return raw or None


def generation() -> int:
    """A counter that changes whenever ``settings.env`` does.

    What a cache holding something derived from configuration compares against.
    It is per-process and per-path and means nothing outside this process — the
    only valid use is comparing it with a value this process read earlier.

    Returns:
        The current generation of the bound file.
    """
    return _current(_store())[2]


def stored_values() -> Mapping[str, str]:
    """What ``settings.env`` currently holds, for the keys this build knows.

    Returns:
        An immutable mapping. It is replaced wholesale on every reload rather
        than mutated, so a caller holding one keeps a consistent view of one
        moment instead of watching it change underneath.
    """
    return _current(_store())[0]


def unreadable_reason() -> str | None:
    """Why ``settings.env`` could not be read, or ``None`` when it could."""
    return _current(_store())[1]


def _resolve_one(
    spec: Setting, stored: Mapping[str, str], unreadable: str | None
) -> tuple[str | None, str]:
    """Apply the ladder to one key against one reading of the file."""
    environment = _environment_value(spec.name)
    if environment is not None:
        return environment, FROM_ENVIRONMENT
    if spec.writable and spec.name in stored:
        return stored[spec.name], FROM_FILE
    if spec.writable and unreadable is not None:
        return spec.default, FROM_UNREADABLE
    return spec.default, FROM_DEFAULT if spec.default is not None else FROM_UNSET


def resolve(name: str) -> str | None:
    """The value in force for ``name``: environment, then ``settings.env``, then the default.

    Empty is not set at any layer, and a name the file layer may not carry
    (:attr:`Setting.writable` false) resolves from the environment and the
    default alone.

    A caller reading more than one value should take a :func:`snapshot` instead:
    read one at a time, two values can come from either side of a write.

    Args:
        name: The setting's name.

    Returns:
        The value in force, stripped of surrounding whitespace, or ``None``
        when nothing set it and it has no default.

    Raises:
        SettingRefused: If nothing by that name is configurable.
    """
    spec = specification(name)
    stored, unreadable, _generation = _current(_store())
    return _resolve_one(spec, stored, unreadable)[0]


def provenance(name: str) -> str:
    """Which layer put the value in force there.

    Args:
        name: The setting's name.

    Returns:
        One of :data:`FROM_ENVIRONMENT`, :data:`FROM_FILE`, :data:`FROM_DEFAULT`,
        :data:`FROM_UNSET` or :data:`FROM_UNREADABLE`.

    Raises:
        SettingRefused: If nothing by that name is configurable.
    """
    spec = specification(name)
    stored, unreadable, _generation = _current(_store())
    return _resolve_one(spec, stored, unreadable)[1]


@dataclass(frozen=True)
class Snapshot:
    """Every setting as it stood at one moment, with the generation that produced it.

    A caller that reads three values one at a time can be overtaken between
    them and act on a mixture of two configurations. One snapshot is one
    configuration, and the generation on it is what a cache compares against —
    taken *before* the values, so a write that lands mid-read leaves the
    snapshot visibly stale rather than invisibly current.
    """

    generation: int
    unreadable: str | None
    values: Mapping[str, str | None]
    provenances: Mapping[str, str]

    def value(self, name: str) -> str | None:
        """The value in force for ``name``, or ``None``.

        Args:
            name: The setting's name.

        Returns:
            The value in force.

        Raises:
            SettingRefused: If nothing by that name is configurable.
        """
        specification(name)
        return self.values[name]

    def provenance(self, name: str) -> str:
        """Which layer put ``name``'s value in force.

        Args:
            name: The setting's name.

        Returns:
            The provenance constant.

        Raises:
            SettingRefused: If nothing by that name is configurable.
        """
        specification(name)
        return self.provenances[name]

    def explicit(self, name: str) -> str | None:
        """The value **somebody set**, ignoring the default.

        The difference from :meth:`value` is load-bearing wherever a caller
        has a defaulting rule of its own that is richer than one constant:
        :mod:`nodum.llm` gives an unset context window the *shipped profile's*
        number rather than its own fallback, and it withholds an API key
        precisely when nobody named an endpoint — both of which read "unset"
        as a fact and would be destroyed by a default arriving in its place.

        Args:
            name: The setting's name.

        Returns:
            The environment's or the file's value, or ``None`` when neither set
            it.

        Raises:
            SettingRefused: If nothing by that name is configurable.
        """
        if self.provenance(name) in (FROM_ENVIRONMENT, FROM_FILE):
            return self.values[name]
        return None

    def source(self, name: str) -> str:
        """A phrase naming where ``name``'s value came from, for a human to act on.

        Args:
            name: The setting's name.

        Returns:
            A noun phrase that reads inside a sentence — "the environment",
            "settings.env", "the built-in default".
        """
        return _SOURCE_PHRASES[self.provenance(name)]


#: How each provenance reads inside a remediation sentence. A message that
#: names a variable an operator has not set sends them to edit the wrong thing,
#: which is the whole reason these strings carry the layer at all.
_SOURCE_PHRASES = {
    FROM_ENVIRONMENT: "the environment",
    FROM_FILE: SETTINGS_FILENAME,
    FROM_DEFAULT: "the built-in default",
    FROM_UNSET: "nothing",
    FROM_UNREADABLE: f"the built-in default — {SETTINGS_FILENAME} could not be read",
}


def snapshot() -> Snapshot:
    """Resolve every setting once, against one reading of the file.

    Returns:
        The frozen :class:`Snapshot`.
    """
    store = _store()
    stored, unreadable, current = _current(store)
    values: dict[str, str | None] = {}
    provenances: dict[str, str] = {}
    for name, spec in SPECS.items():
        values[name], provenances[name] = _resolve_one(spec, stored, unreadable)
    return Snapshot(
        generation=current,
        unreadable=unreadable,
        values=MappingProxyType(values),
        provenances=MappingProxyType(provenances),
    )


# ── Writing ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Change:
    """What one write did to ``settings.env``, before and after.

    ``before`` and ``after`` are what the *file* held, not what was in force:
    an event about a settings write is about the file, and the environment on
    top of it is a different fact that a reader can go and check.
    """

    key: str
    before: str | None
    after: str | None

    @property
    def changed(self) -> bool:
        """Whether the file's contents for this key actually moved."""
        return self.before != self.after

    def event_payload(self) -> dict[str, str | None]:
        """The audit payload for this change, with any secret reduced to set/unset.

        The only supported way to build one. A caller that assembled the dict
        itself would be one refactor away from writing an API key into an
        append-only log that every projector rebuild reads end to end.

        Returns:
            ``{"key", "before", "after"}``, where a secret's values are the
            words ``"set"`` and ``"unset"`` rather than anything it held.
        """
        return {
            "key": self.key,
            "before": redact(self.key, self.before),
            "after": redact(self.key, self.after),
        }


def redact(name: str, value: str | None) -> str | None:
    """Reduce a secret to whether it is set; pass anything else through.

    Args:
        name: The setting's name.
        value: The value to render.

    Returns:
        ``None`` for an unset non-secret, the value itself for a set one, and
        ``"set"`` / ``"unset"`` for a secret.
    """
    if name in SECRET_KEYS:
        return "set" if value else "unset"
    return value


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive ``flock`` on ``path`` for the block.

    ``flock`` rather than an ``O_EXCL`` lockfile with a pid and an age in it:
    the kernel drops the lock when the holder dies, so there is no stale-lock
    apparatus to get wrong, and a pid inside a container's namespace is not a
    pid anybody outside it can check.

    ``fcntl`` is imported here rather than at module scope because it is
    POSIX-only. Nothing else in this module needs it, so importing it lazily
    keeps the package importable where it does not exist and turns the absence
    into a failure of the one operation that needs it.

    The lockfile's directory is created here rather than by the write that
    follows: the lock is taken *first*, so binding to a data directory that
    does not exist yet — a fresh install writing a setting before ``init`` —
    failed on the lockfile with a bare ``FileNotFoundError`` before anything
    could create it.
    """
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _replace_atomically(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` so a reader sees all of it or none of it.

    The temp file is created in the *same directory* — ``os.replace`` across a
    mount boundary is ``EXDEV``, and the deployment bind-mounts the data
    directory — and created 0600 rather than chmod-ed afterwards, so the file
    is never briefly world-readable while it holds an API key. Then: fsync the
    file (``os.replace`` orders the rename, not the data behind it), replace,
    and fsync the directory so the rename itself survives a power cut.
    """
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".{path.name}.{os.getpid()}.{token_hex(6)}"
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        # Through a buffered file object rather than a bare `os.write`: that
        # call returns how many bytes it took and is permitted to take fewer,
        # and an unchecked short write here would be fsynced and renamed into
        # place as a truncated settings file. `closefd=False` keeps the
        # descriptor this function opened, so the mode it was created with is
        # the mode the file keeps.
        with open(descriptor, "wb", closefd=False) as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(descriptor)
        os.close(descriptor)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    directory_descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _write(name: str, value: str | None) -> Change:
    """Merge one key into ``settings.env`` under both writer locks, and publish the result.

    ``write_lock`` and the ``flock`` are held across the whole span; the cache
    lock is not, so a reader resolving while this waits on another process's
    lock answers immediately out of the view it already has.
    """
    store = _store()
    if store is None:
        raise SettingRefused(f"no graph is bound, so there is nowhere to keep {SETTINGS_FILENAME}")
    with store.write_lock, _file_lock(store.lock_path):
        # Read from the disk rather than from the cache: the cache is a view of
        # some earlier moment, and merging into it would drop whatever another
        # process wrote since.
        try:
            text = store.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        try:
            before, _unknown = _parse(text)
        except (UnicodeDecodeError, SettingsFileUnreadable) as exc:
            raise SettingsFileUnreadable(
                f"{store.path} cannot be parsed, so it cannot be edited without losing "
                f"what it holds ({exc}); fix the file by hand"
            ) from exc
        previous = before.get(name)
        _replace_atomically(store.path, _render(text, name, value))
        # Force the next read to reload: the stamp of what was just written is
        # not known here, and inventing one would be a cache that agrees with a
        # file it never read.
        store.stamp = _NEVER_READ
        _refresh(store)
    return Change(key=name, before=previous, after=value)


def set_value(name: str, value: str) -> Change:
    """Store one setting in ``settings.env``.

    The value is validated **before** it is written, so the file never holds
    something the runtime would go on to discard — the accepted-but-inert edit
    is the failure this door exists to prevent.

    Args:
        name: The setting's name.
        value: Its new value.

    Returns:
        The :class:`Change` the file underwent.

    Raises:
        SettingRefused: If the name is unknown, cannot be stored here, is
            pinned by a non-empty environment variable, or the value is empty,
            carries a control character or a newline, or fails the key's own
            validation.
        SettingsFileUnreadable: If the existing file cannot be parsed, and so
            cannot be rewritten without losing what it holds.
    """
    spec = specification(name)
    if not spec.writable:
        raise SettingRefused(f"{name} cannot be stored in {SETTINGS_FILENAME}: {spec.refusal}")
    if _environment_value(name) is not None:
        raise SettingRefused(
            f"{name} is set in the environment, which wins over {SETTINGS_FILENAME}; "
            f"a value stored here would never be used — unset the environment variable first"
        )
    cleaned = value.strip()
    if not cleaned:
        raise SettingRefused(
            f"{name} cannot be set to an empty value; unset it instead to fall back"
        )
    if _CONTROL_CHARACTERS.search(cleaned):
        # A newline in a value is not a formatting problem: it is one more
        # `KEY=value` line, chosen by whoever supplied the value.
        raise SettingRefused(f"{name} may not contain control characters or newlines")
    return _write(name, spec.validate(name, cleaned))


def unset_value(name: str) -> Change:
    """Remove one setting from ``settings.env``, falling back to the default.

    Removing a key that is not there is not an error — it is the state the
    caller asked for — and answers with ``changed`` false.

    Args:
        name: The setting's name.

    Returns:
        The :class:`Change` the file underwent.

    Raises:
        SettingRefused: If the name is unknown or cannot be stored here.
        SettingsFileUnreadable: If the existing file cannot be parsed.
    """
    spec = specification(name)
    if not spec.writable:
        raise SettingRefused(f"{name} is not stored in {SETTINGS_FILENAME}: {spec.refusal}")
    return _write(name, None)


# ── What the surfaces report ─────────────────────────────────────────────────


def _describe(name: str, view: Snapshot, stored: Mapping[str, str]) -> SettingOut:
    """One row of the settings report, with any secret reduced to whether it is set."""
    spec = SPECS[name]
    value = view.value(name)
    return SettingOut(
        key=name,
        value=None if spec.secret else value,
        set=value is not None,
        provenance=view.provenance(name),
        default=None if spec.secret else spec.default,
        kind=spec.kind,
        secret=spec.secret,
        writable=spec.writable,
        refusal=spec.refusal,
        stored=name in stored,
        on_invalid=spec.on_invalid,
    )


def get_setting(name: str) -> SettingOut:
    """Report one setting: what is in force, where it came from, whether it can be set.

    A secret's value is never carried — the row says whether one is set and
    nothing more.

    Args:
        name: The setting's name.

    Returns:
        The :class:`~nodum.models.SettingOut` row.

    Raises:
        SettingRefused: If nothing by that name is configurable.
    """
    specification(name)
    return _describe(name, snapshot(), stored_values())


def list_settings() -> SettingsOut:
    """Report every setting, plus the file they are read from.

    Returns:
        The :class:`~nodum.models.SettingsOut` envelope: the rows, the bound
        path, any keys the file carries that this build does not configure, and
        why the file could not be read when it could not.
    """
    view = snapshot()
    stored = stored_values()
    store = _store()
    return SettingsOut(
        settings=[_describe(name, view, stored) for name in KEYS],
        count=len(KEYS),
        path=None if store is None else str(store.path),
        unknown_keys=list(() if store is None else store.unknown),
        unreadable=view.unreadable,
    )


def describe_change(change: Change) -> SettingChangeOut:
    """Report what a write left in force, carrying no secret value.

    Args:
        change: The change :func:`set_value` or :func:`unset_value` returned.

    Returns:
        The :class:`~nodum.models.SettingChangeOut` row.
    """
    spec = SPECS[change.key]
    view = snapshot()
    value = view.value(change.key)
    return SettingChangeOut(
        key=change.key,
        changed=change.changed,
        stored=change.after is not None,
        value=None if spec.secret else value,
        set=value is not None,
        provenance=view.provenance(change.key),
    )
