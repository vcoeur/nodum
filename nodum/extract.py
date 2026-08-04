"""Text extraction: MIME → text, through handlers that degrade instead of failing.

Phase 4 note 01 D2: extraction is a registry of **optional** handlers shaped
exactly like the embedding provider seam (:mod:`nodum.embeddings`). Each
handler declares the MIME families it claims and whether it can run, and an
absent dependency is a *returned result*, never an exception:
:func:`extract` on a machine with no OCR still returns an
:class:`Extraction`, so ingestion still registers the asset, still writes the
describing node, and says plainly in ``detail`` that no text came out. That is
the design decision this module exists to enforce — ingestion has to converge
on every machine, and the half that could not run has to be legible rather
than fatal. The same rule covers a *broken input*: a corrupt PDF is a
``detail`` string, not a traceback climbing out of the pipeline.

Registry order is ``text``, ``html``, ``pdf``, ``image``, ``audio``, and the
first handler claiming the MIME wins. ``text`` claims the whole ``text/*``
family plus JSON but deliberately stands aside for the two HTML types
(:data:`HTML_MIMES`), which the next handler in the order parses properly —
otherwise markup would land in the graph as literal tags. ``text`` and
``html`` are stdlib-only and therefore **always available**, which is what
makes the pipeline end-to-end before any heavy dependency lands; ``pdf``
(``pypdf``), ``image`` (``pytesseract`` *and* the tesseract binary) and
``audio`` (``faster-whisper``) sit behind ``pyproject`` extras and report
themselves unavailable until installed.

``video/*`` is deliberately **not claimed**. Pulling text out of a video means
demuxing its audio track with ffmpeg — a non-Python binary this project does
not otherwise need — to produce a transcript of a file whose visual content is
usually the point. A video handler belongs behind its own extra once there is
a use for one; until then an unclaimed MIME says so honestly
(``handler="none"``) rather than half-working.

Every result is capped at :data:`MAX_TEXT_CHARS`, because the extracted text
becomes a database row and one pathological file must not be able to make that
row unbounded. When the cap bites it is reported in ``detail`` — truncation is
never silent.
"""

from __future__ import annotations

import codecs
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol

from nodum.models import HandlerStatus

#: Largest extracted text kept from one asset.
#:
#: ~2 MB of characters is roughly a 600-page book — far above anything a
#: personal knowledge base ingests, and still small enough that the row, the
#: FTS insert, and the chunking pass over it stay cheap. Beyond it the text is
#: truncated and ``detail`` says so.
MAX_TEXT_CHARS = 2_000_000

#: The two MIME types the ``html`` handler owns. Named here because the
#: ``text`` handler has to exclude them from its ``text/*`` claim.
HTML_MIMES = ("text/html", "application/xhtml+xml")

#: How per-page texts are joined into a paginated extraction's ``text``.
PAGE_SEPARATOR = "\n\n"

#: The OCR binary ``pytesseract`` shells out to. Its absence is a different
#: failure from the module's absence, and gets a different explanation.
TESSERACT_BINARY = "tesseract"

#: Whisper model size used for audio transcription. ``base`` is the smallest
#: model that produces usable prose.
AUDIO_MODEL = "base"

#: Environment override for the transcription model name.
ENV_AUDIO_MODEL_VAR = "NODUM_AUDIO_MODEL"

#: Environment flag allowing the one-time transcription-model download.
#:
#: The same posture :mod:`nodum.embeddings` takes with ``NODUM_EMBED_DOWNLOAD``,
#: and for the same reason: a knowledge tool must not silently pull hundreds of
#: megabytes off the network because someone ingested an ``.mp3``. Without it
#: faster-whisper is held to its local cache, and an uncached model surfaces as
#: an unavailable handler rather than a download.
ENV_AUDIO_DOWNLOAD_VAR = "NODUM_AUDIO_DOWNLOAD"

#: Ceiling on a ``detail`` string.
#:
#: ``detail`` is frequently the ``str()`` of a third-party exception, and it
#: travels into an event payload and a result envelope. A parser that embeds
#: the offending document in its message would otherwise write an unbounded
#: row, so the explanation is bounded at something a human still reads.
MAX_DETAIL_CHARS = 500


@dataclass(frozen=True)
class Extraction:
    """What one handler got out of one file.

    ``handler`` is the handler's name, or ``"none"`` when no handler claimed
    the MIME. ``text`` is ``""`` whenever nothing came out — a missing
    dependency, an empty file, a scanned PDF — and ``detail`` then says why.
    ``pages`` carries per-page text for paginated formats (index ``n - 1`` is
    page ``n``, empty pages included so the numbering holds) and is empty for
    everything else.
    """

    handler: str
    text: str
    pages: list[str] = field(default_factory=list)
    detail: str | None = None


class Handler(Protocol):
    """A MIME family's extractor: does it claim this type, can it run, what came out."""

    name: str
    mimes: tuple[str, ...]

    def handles(self, mime: str) -> bool:
        """Return whether this handler claims ``mime``."""
        ...

    def availability(self) -> tuple[bool, str | None]:
        """Return ``(available, reason)`` — ``reason`` explains an unavailable handler."""
        ...

    def extract(self, source: Path, *, mime: str) -> Extraction:
        """Extract text from ``source``, which the caller has checked this handler claims."""
        ...


# ── MIME matching ────────────────────────────────────────────────────────────


def _normalize_mime(mime: str) -> str:
    """Reduce a MIME to bare lowercase ``type/subtype``, dropping parameters and spacing."""
    return mime.split(";", 1)[0].strip().lower()


def _declared_charset(mime: str, default: str = "utf-8") -> str:
    """Return the ``charset`` parameter of a full Content-Type, or ``default``.

    An unregistered codec name falls back rather than raising: a server is free
    to declare a charset Python has never heard of, and that is not a reason to
    refuse the document.
    """
    for parameter in mime.split(";")[1:]:
        key, separator, value = parameter.partition("=")
        if separator and key.strip().lower() == "charset":
            candidate = value.strip().strip('"').lower()
            try:
                codecs.lookup(candidate)
            except LookupError:
                return default
            return candidate
    return default


class _BaseHandler:
    """Shared MIME matching and the default "always available" answer.

    Subclasses set :attr:`name` and :attr:`mimes` — exact types, or a
    ``family/*`` pattern — and override :meth:`availability` when they depend
    on something that can be absent.
    """

    #: Registry key, and the ``handler`` field of everything this returns.
    name: str = ""

    #: Claimed MIME families: exact types, or ``family/*`` prefixes.
    mimes: tuple[str, ...] = ()

    def handles(self, mime: str) -> bool:
        """Return whether ``mime`` (parameters ignored) falls in :attr:`mimes`."""
        normalized = _normalize_mime(mime)
        return any(
            normalized.startswith(pattern[:-1]) if pattern.endswith("/*") else normalized == pattern
            for pattern in self.mimes
        )

    def availability(self) -> tuple[bool, str | None]:
        """Available unconditionally; the optional handlers override this."""
        return (True, None)


# ── Result construction (the text cap lives here) ────────────────────────────


def _cap_text(text: str) -> tuple[str, str | None]:
    """Truncate ``text`` to :data:`MAX_TEXT_CHARS`, explaining the cut when there was one."""
    if len(text) <= MAX_TEXT_CHARS:
        return text, None
    return text[:MAX_TEXT_CHARS], (
        f"text truncated to the {MAX_TEXT_CHARS}-character cap "
        f"(the source held {len(text)} characters)"
    )


def _cap_pages(pages: list[str]) -> tuple[list[str], str | None]:
    """Keep whole pages up to the cap, truncating the page that crosses it.

    Pages are capped rather than the joined text so the two stay consistent: a
    caller writing one block per page must never be handed page text that the
    capped ``text`` dropped.
    """
    kept: list[str] = []
    budget = MAX_TEXT_CHARS
    for index, page in enumerate(pages):
        if budget <= 0:
            return kept, (
                f"stopped after page {index} of {len(pages)}: the "
                f"{MAX_TEXT_CHARS}-character text cap was reached"
            )
        if len(page) > budget:
            kept.append(page[:budget])
            return kept, (
                f"page {index + 1} of {len(pages)} truncated at the "
                f"{MAX_TEXT_CHARS}-character text cap"
            )
        kept.append(page)
        budget -= len(page) + len(PAGE_SEPARATOR)
    return kept, None


def _flat_extraction(handler_name: str, text: str, *, empty_detail: str) -> Extraction:
    """Build an unpaginated result: cap the text, and explain an empty one."""
    capped, detail = _cap_text(text)
    if detail is None and not capped.strip():
        # "Nothing came out" is `text == ""`, never a body of whitespace: a
        # caller testing the text must not have to strip it first.
        return Extraction(handler=handler_name, text="", detail=empty_detail)
    return Extraction(handler=handler_name, text=capped, detail=detail)


# ── Handlers ─────────────────────────────────────────────────────────────────


class TextHandler(_BaseHandler):
    """Plain text, Markdown, CSV, JSON — anything textual that is not markup.

    Stdlib only, so always available. Bytes are decoded as UTF-8 with
    replacement: an extractor whose job is "get the text out" must not fail on
    a stray byte, so a bad one becomes U+FFFD and the rest of the document
    still lands.
    """

    name = "text"
    mimes: tuple[str, ...] = ("text/*", "application/json")

    def handles(self, mime: str) -> bool:
        """Claim the text family and JSON — but leave HTML to the ``html`` handler."""
        normalized = _normalize_mime(mime)
        if normalized in HTML_MIMES:
            return False
        return super().handles(normalized)

    def extract(self, source: Path, *, mime: str) -> Extraction:
        """Read ``source`` as UTF-8 text, replacing undecodable bytes."""
        text = source.read_bytes().decode("utf-8", errors="replace")
        return _flat_extraction(self.name, text, empty_detail="the file holds no text")


#: Any run of whitespace in HTML text, which renders as a single space.
_WHITESPACE_RUN = re.compile(r"\s+")

#: HTML elements whose entire body is dropped, not just their tags.
_SKIPPED_ELEMENTS = frozenset({"script", "style", "noscript", "template"})

#: HTML elements that force a line break, so block content stays separated.
_BLOCK_TAGS = (
    "address article aside blockquote br dd div dl dt figcaption figure footer form "
    "h1 h2 h3 h4 h5 h6 header hr li main nav ol p pre section table tbody td tfoot "
    "th thead title tr ul"
)
_BLOCK_ELEMENTS = frozenset(_BLOCK_TAGS.split())


class _HtmlTextParser(HTMLParser):
    """Collect an HTML document's visible text, dropping script and style bodies.

    ``<script>``/``<style>`` content is *code*, not prose: indexing it would
    put minified JavaScript into search results. Block-level tags emit a break
    so paragraphs do not run into each other, and everything else collapses to
    single spaces.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        # Nesting depth inside a skipped subtree, so a <script> holding a
        # nested tag still suppresses everything under it.
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Enter a skipped subtree, or emit a break for a block-level element."""
        if tag in _SKIPPED_ELEMENTS:
            self._skip_depth += 1
        elif tag in _BLOCK_ELEMENTS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        """Leave a skipped subtree, or close a block-level element with a break."""
        if tag in _SKIPPED_ELEMENTS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_ELEMENTS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        """Collect text, unless it belongs to a skipped subtree."""
        if self._skip_depth == 0:
            # Whitespace is collapsed here, not in `text`, so that source line
            # breaks inside a paragraph stay spaces and the only newlines left
            # in `_parts` are the block markers above. The run is collapsed to
            # one space rather than dropped: it may be the only thing keeping
            # two inline elements' words apart.
            self._parts.append(_WHITESPACE_RUN.sub(" ", data))

    def text(self) -> str:
        """Return the collected text: blocks on their own lines, whitespace collapsed."""
        lines = ("".join(self._parts)).split("\n")
        return "\n".join(stripped for line in lines if (stripped := line.strip()))


class HtmlHandler(_BaseHandler):
    """HTML and XHTML, through a stdlib :mod:`html.parser` subclass.

    Always available — the parser is stdlib, and a saved web page is a common
    enough ingestion input that it must work on a bare install. Entities are
    unescaped by the parser (``convert_charrefs``); markup, script bodies and
    style bodies never reach the text.
    """

    name = "html"
    mimes: tuple[str, ...] = HTML_MIMES

    def extract(self, source: Path, *, mime: str) -> Extraction:
        """Parse ``source`` and return its visible text.

        The declared ``charset`` parameter is honoured when the caller passes a
        full ``Content-Type``: a latin-1 page decoded as UTF-8 comes back with
        replacement characters exactly where its accents were, which is silent
        corruption of the text the whole pipeline then indexes. An unknown or
        absent charset falls back to UTF-8 with replacement, which is the right
        guess for the modern web.
        """
        markup = source.read_bytes().decode(_declared_charset(mime), errors="replace")
        parser = _HtmlTextParser()
        parser.feed(markup)
        parser.close()
        return _flat_extraction(
            self.name, parser.text(), empty_detail="the document holds no visible text"
        )


class PdfHandler(_BaseHandler):
    """PDF text through ``pypdf`` (the ``pdf`` extra), one entry per page.

    Only *embedded* text is read: a scanned PDF is a sequence of images and
    comes back empty with a ``detail`` pointing at OCR, rather than looking
    like an empty document.
    """

    name = "pdf"
    mimes: tuple[str, ...] = ("application/pdf",)

    def availability(self) -> tuple[bool, str | None]:
        """Available when ``pypdf`` imports."""
        return _probe(self.name, _probe_pypdf)

    def extract(self, source: Path, *, mime: str) -> Extraction:
        """Return per-page text, and the pages joined by a blank line as ``text``."""
        from pypdf import PdfReader

        reader = PdfReader(str(source))
        # Empty pages are kept: `pages[n - 1]` has to stay page n for the
        # per-page blocks and `page:<n>` rasters built on top of this.
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
        kept, detail = _cap_pages(pages)
        text = PAGE_SEPARATOR.join(kept)
        if not text.strip():
            # A document of empty pages joins into separators; "nothing came
            # out" has to read as `text == ""` there too.
            text = ""
            if detail is None:
                detail = (
                    f"no embedded text in {len(pages)} page(s) — a scanned PDF "
                    f"needs the image handler's OCR pass"
                )
        return Extraction(handler=self.name, text=text, pages=kept, detail=detail)


class ImageHandler(_BaseHandler):
    """OCR for images through ``pytesseract`` (the ``ocr`` extra).

    Availability is two conditions, reported apart: ``pytesseract`` is a thin
    wrapper that shells out, so the Python package being installed says
    nothing about the ``tesseract`` binary being on PATH — and "install the
    extra" is the wrong advice for the second case.
    """

    name = "image"
    mimes: tuple[str, ...] = ("image/*",)

    def availability(self) -> tuple[bool, str | None]:
        """Available when ``pytesseract`` imports *and* the tesseract binary is on PATH."""
        return _probe(self.name, _probe_pytesseract)

    def extract(self, source: Path, *, mime: str) -> Extraction:
        """Run OCR over the image and return the recognised text."""
        import pytesseract  # pyright: ignore[reportMissingImports] degraded-mode
        from PIL import Image

        with Image.open(source) as image:
            text = pytesseract.image_to_string(image)
        return _flat_extraction(self.name, text, empty_detail="OCR recognised no text in the image")


class AudioHandler(_BaseHandler):
    """Speech-to-text through ``faster-whisper`` (the ``audio`` extra)."""

    name = "audio"
    mimes: tuple[str, ...] = ("audio/*",)

    def availability(self) -> tuple[bool, str | None]:
        """Available when ``faster_whisper`` imports."""
        return _probe(self.name, _probe_faster_whisper)

    def extract(self, source: Path, *, mime: str) -> Extraction:
        """Transcribe the audio and return the segments as one text.

        The model is **never fetched implicitly** — the rule
        :mod:`nodum.embeddings` already holds the embedding model to. Without
        ``NODUM_AUDIO_DOWNLOAD=1`` faster-whisper is confined to its local
        cache, and an uncached model raises here, which :func:`extract` turns
        into a ``detail`` naming the flag rather than a silent download.
        """
        from faster_whisper import (  # pyright: ignore[reportMissingImports] degraded-mode
            WhisperModel,
        )

        model = WhisperModel(
            os.environ.get(ENV_AUDIO_MODEL_VAR, AUDIO_MODEL),
            device="cpu",
            compute_type="int8",
            local_files_only=os.environ.get(ENV_AUDIO_DOWNLOAD_VAR) != "1",
        )
        segments, _info = model.transcribe(str(source))
        text = " ".join(segment.text.strip() for segment in segments)
        return _flat_extraction(
            self.name, text, empty_detail="the transcript is empty (no recognised speech)"
        )


#: Handlers in claim order — the first one claiming a MIME wins.
REGISTRY: tuple[Handler, ...] = (
    TextHandler(),
    HtmlHandler(),
    PdfHandler(),
    ImageHandler(),
    AudioHandler(),
)


# ── Availability probes (cached process-wide) ────────────────────────────────

_probes: dict[str, tuple[bool, str | None]] = {}


def _probe(name: str, probe: Callable[[], tuple[bool, str | None]]) -> tuple[bool, str | None]:
    """Run ``probe`` at most once per process, caching its answer under ``name``.

    Same posture as :func:`nodum.embeddings.get_provider`: an availability
    check is asked on every ingestion, so importing an optional package (or
    searching PATH) must not be repeated. :func:`reset_availability` is the
    test and reconfiguration seam.
    """
    if name not in _probes:
        _probes[name] = probe()
    return _probes[name]


def _probe_pypdf() -> tuple[bool, str | None]:
    """Check for the PDF text-extraction dependency."""
    try:
        import pypdf  # noqa: F401
    except ImportError:
        return False, "pypdf is not installed (install the 'pdf' extra)"
    return True, None


def _probe_pytesseract() -> tuple[bool, str | None]:
    """Check for the OCR wrapper and, separately, the binary it drives."""
    try:
        import pytesseract  # noqa: F401  # pyright: ignore[reportMissingImports] degraded-mode
    except ImportError:
        return False, "pytesseract is not installed (install the 'ocr' extra)"
    if shutil.which(TESSERACT_BINARY) is None:
        return False, (
            f"the {TESSERACT_BINARY} binary is not on PATH — pytesseract only wraps it, "
            f"so install tesseract itself through the system package manager"
        )
    return True, None


def _probe_faster_whisper() -> tuple[bool, str | None]:
    """Check for the speech-to-text dependency."""
    try:
        import faster_whisper  # noqa: F401  # pyright: ignore[reportMissingImports] degraded-mode
    except ImportError:
        return False, "faster-whisper is not installed (install the 'audio' extra)"
    return True, None


def reset_availability() -> None:
    """Drop every cached availability probe; the next check re-probes from scratch."""
    _probes.clear()


# ── Public API ───────────────────────────────────────────────────────────────


def handler_for(mime: str) -> Handler | None:
    """Return the first registered handler claiming ``mime``, or ``None``.

    MIME parameters are ignored, so ``text/html; charset=utf-8`` resolves the
    same as ``text/html``. Availability is *not* consulted: a handler that
    claims the type but cannot run is still the right handler to report.
    """
    normalized = _normalize_mime(mime)
    for handler in REGISTRY:
        if handler.handles(normalized):
            return handler
    return None


def extract(source: Path, *, mime: str) -> Extraction:
    """Extract text from ``source``, never raising.

    Every outcome is an :class:`Extraction`: no handler for the type, a
    handler whose dependency is absent, a file the handler choked on, and the
    successful case all come back the same shape, with ``detail`` carrying the
    explanation whenever ``text`` is empty or partial. This is what lets
    ingestion register an asset and describe it even when extraction got
    nothing (note 01 D2).

    Args:
        source: Path to the file to read.
        mime: The file's MIME type; parameters are ignored.

    Returns:
        The extraction result — ``handler="none"`` when nothing claimed the type.
    """
    normalized = _normalize_mime(mime)
    handler = handler_for(normalized)
    if handler is None:
        return Extraction(handler="none", text="", detail=f"no extraction handler for {normalized}")
    available, reason = handler.availability()
    if not available:
        return Extraction(handler=handler.name, text="", detail=reason)
    try:
        # The *full* type goes to the handler, parameters included: the claim
        # was decided on the bare type, but `charset=` is the handler's to read.
        return handler.extract(source, mime=mime)
    except Exception as exc:
        # A corrupt or surprising file is a reportable result, not a pipeline
        # failure: the asset is still registered and still describable.
        return Extraction(
            handler=handler.name,
            text="",
            detail=_bounded_detail(
                f"{handler.name} extraction failed: {type(exc).__name__}: {exc}"
            ),
        )


def _bounded_detail(detail: str) -> str:
    """Clip an explanation to :data:`MAX_DETAIL_CHARS`, marking it when clipped."""
    if len(detail) <= MAX_DETAIL_CHARS:
        return detail
    return detail[:MAX_DETAIL_CHARS] + " …"


def availability() -> list[HandlerStatus]:
    """Report every registered handler's reach and whether it can run, in registry order."""
    statuses: list[HandlerStatus] = []
    for handler in REGISTRY:
        available, reason = handler.availability()
        statuses.append(
            HandlerStatus(
                name=handler.name,
                mimes=list(handler.mimes),
                available=available,
                detail=reason,
            )
        )
    return statuses
