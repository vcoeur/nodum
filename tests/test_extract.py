"""The extraction registry: MIME routing, degradation, and the text cap (nodum.extract)."""

from __future__ import annotations

import importlib.util
import shutil
import sys
import types

import pytest

from nodum import extract

#: The `pdf` extra is optional and CI runs the base install, so every test
#: that needs a real PDF parser skips cleanly without it.
requires_pypdf = pytest.mark.skipif(
    importlib.util.find_spec("pypdf") is None,
    reason="the 'pdf' extra is not installed",
)


@pytest.fixture(autouse=True)
def _fresh_availability():
    """Availability probes are cached process-wide; each test gets a clean cache."""
    extract.reset_availability()
    yield
    extract.reset_availability()


def _minimal_pdf(page_texts: list[str]) -> bytes:
    """Build a tiny valid PDF whose pages carry ``page_texts``, one text run each.

    Written by hand rather than committed as a binary fixture: this only has to
    prove the handler reads per-page text, and a generated file keeps the test
    readable and the repository free of opaque blobs.
    """
    font_number = 3 + 2 * len(page_texts)
    kids = " ".join(f"{3 + 2 * index} 0 R" for index in range(len(page_texts)))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_texts)} >>".encode(),
    ]
    for index, page_text in enumerate(page_texts):
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
            f"/Contents {4 + 2 * index} 0 R "
            f"/Resources << /Font << /F1 {font_number} 0 R >> >> >>".encode()
        )
        stream = f"BT /F1 12 Tf 20 100 Td ({page_text}) Tj ET".encode()
        objects.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n"
    ).encode()
    return bytes(out)


# ── MIME routing ─────────────────────────────────────────────────────────────


def test_the_text_family_and_json_go_to_the_text_handler():
    for mime in ("text/plain", "text/markdown", "text/csv", "text/x-rst", "application/json"):
        handler = extract.handler_for(mime)
        assert handler is not None, mime
        assert handler.name == "text", mime


def test_markup_goes_to_the_html_handler_not_the_text_handler():
    """`text` is first in the registry and claims `text/*` — but stands aside for markup."""
    for mime in extract.HTML_MIMES:
        handler = extract.handler_for(mime)
        assert handler is not None
        assert handler.name == "html"
    assert extract.TextHandler().handles("text/html") is False


def test_mime_parameters_and_case_do_not_change_the_handler(tmp_path):
    """A served type carries parameters; matching normalises them away."""
    assert extract.handler_for("text/html; charset=utf-8").name == "html"
    assert extract.handler_for("  TEXT/Plain ").name == "text"

    path = tmp_path / "note.txt"
    path.write_text("body", encoding="utf-8")
    assert extract.extract(path, mime="text/plain; charset=UTF-8").text == "body"


def test_binary_families_resolve_by_family_prefix():
    assert extract.handler_for("application/pdf").name == "pdf"
    assert extract.handler_for("image/png").name == "image"
    assert extract.handler_for("image/jpeg").name == "image"
    assert extract.handler_for("audio/mpeg").name == "audio"


def test_video_is_deliberately_unclaimed(tmp_path):
    """Note 01 D2 stops at audio: a video handler needs ffmpeg and has no use yet."""
    assert extract.handler_for("video/mp4") is None
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00\x01")

    result = extract.extract(path, mime="video/mp4")

    assert result.handler == "none"
    assert result.text == ""
    assert "video/mp4" in result.detail


def test_an_unknown_mime_has_no_handler_and_says_so(tmp_path):
    path = tmp_path / "thing.bin"
    path.write_bytes(b"\x00")

    result = extract.extract(path, mime="application/x-thing")

    assert result.handler == "none"
    assert result.pages == []
    assert result.detail == "no extraction handler for application/x-thing"


# ── The text handler ─────────────────────────────────────────────────────────


def test_text_handler_reads_utf8_content(tmp_path):
    path = tmp_path / "note.md"
    path.write_text("# Photosynthesis\n\nPlants convert sunlight — really.\n", encoding="utf-8")

    result = extract.extract(path, mime="text/markdown")

    assert result.handler == "text"
    assert "Plants convert sunlight — really." in result.text
    assert result.pages == []
    assert result.detail is None


def test_undecodable_bytes_are_replaced_not_raised(tmp_path):
    """Extraction's job is to get text out; a stray byte must not end the run."""
    path = tmp_path / "mixed.txt"
    path.write_bytes(b"before \xff\xfe after")

    result = extract.extract(path, mime="text/plain")

    assert result.handler == "text"
    assert result.text.startswith("before ")
    assert result.text.endswith(" after")
    assert "�" in result.text


def test_an_empty_file_reports_why_the_text_is_empty(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("   \n\n", encoding="utf-8")

    result = extract.extract(path, mime="text/plain")

    assert result.handler == "text"
    assert result.text == ""  # never a body of whitespace
    assert result.detail == "the file holds no text"


# ── The html handler ─────────────────────────────────────────────────────────


def test_html_never_leaks_script_or_style_bodies(tmp_path):
    """Script and style bodies are code: indexing them would poison search."""
    path = tmp_path / "page.html"
    path.write_text(
        "<html><head><style>body{color:#c0ffee}</style>"
        "<script>var apiKey = 'SHOULD_NOT_LEAK'; if (1 < 2) { hide(); }</script></head>"
        "<body><p>Visible prose.</p><noscript>enable scripts</noscript></body></html>",
        encoding="utf-8",
    )

    result = extract.extract(path, mime="text/html")

    assert result.handler == "html"
    assert result.text == "Visible prose."
    for leak in ("SHOULD_NOT_LEAK", "apiKey", "c0ffee", "enable scripts"):
        assert leak not in result.text


def test_html_unescapes_entities_and_collapses_whitespace(tmp_path):
    path = tmp_path / "page.html"
    path.write_text(
        "<html><body><p>Salt   &amp;\n\n  pepper &lt;3 &#233;t&eacute;</p></body></html>",
        encoding="utf-8",
    )

    result = extract.extract(path, mime="text/html")

    assert result.text == "Salt & pepper <3 été"


def test_html_keeps_paragraphs_apart(tmp_path):
    """Without block breaks, "…first.Second…" is what reaches the graph."""
    path = tmp_path / "page.html"
    path.write_text(
        "<html><body><h1>Title</h1><p>First paragraph.</p><p>Second paragraph.</p>"
        "<ul><li>one</li><li>two</li></ul><div>tail<br>after break</div></body></html>",
        encoding="utf-8",
    )

    result = extract.extract(path, mime="text/html")

    assert result.text.split("\n") == [
        "Title",
        "First paragraph.",
        "Second paragraph.",
        "one",
        "two",
        "tail",
        "after break",
    ]


def test_html_with_no_visible_text_says_so(tmp_path):
    path = tmp_path / "page.html"
    path.write_text("<html><body><script>only();</script></body></html>", encoding="utf-8")

    result = extract.extract(path, mime="application/xhtml+xml")

    assert result.handler == "html"
    assert result.text == ""
    assert result.detail == "the document holds no visible text"


# ── Absent dependencies: the path most installs take (note 01 D2) ────────────


def test_the_stdlib_handlers_are_always_available():
    """`text` and `html` have nothing to be missing — that is what makes the pipeline land."""
    statuses = {status.name: status for status in extract.availability()}
    for name in ("text", "html"):
        assert statuses[name].available is True
        assert statuses[name].detail is None


def test_missing_pypdf_is_a_result_not_an_exception(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pypdf", None)  # makes `import pypdf` raise
    extract.reset_availability()
    path = tmp_path / "paper.pdf"
    path.write_bytes(_minimal_pdf(["ignored"]))

    result = extract.extract(path, mime="application/pdf")

    assert result.handler == "pdf"
    assert result.text == ""
    assert result.pages == []
    assert "pypdf is not installed" in result.detail
    assert "'pdf' extra" in result.detail


def test_missing_pytesseract_names_the_ocr_extra(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pytesseract", None)
    extract.reset_availability()
    path = tmp_path / "scan.png"
    path.write_bytes(b"not really a png")

    result = extract.extract(path, mime="image/png")

    assert result.handler == "image"
    assert result.text == ""
    assert "pytesseract is not installed" in result.detail
    assert "'ocr' extra" in result.detail


def test_a_missing_tesseract_binary_is_a_different_message(tmp_path, monkeypatch):
    """pytesseract only wraps a binary, so "install the extra" is the wrong advice here."""
    monkeypatch.setitem(sys.modules, "pytesseract", types.ModuleType("pytesseract"))
    monkeypatch.setattr(shutil, "which", lambda name: None)
    extract.reset_availability()
    path = tmp_path / "scan.png"
    path.write_bytes(b"not really a png")

    result = extract.extract(path, mime="image/png")

    assert result.handler == "image"
    assert result.text == ""
    assert extract.TESSERACT_BINARY in result.detail
    assert "PATH" in result.detail
    assert "pytesseract is not installed" not in result.detail


def test_missing_faster_whisper_names_the_audio_extra(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    extract.reset_availability()
    path = tmp_path / "memo.mp3"
    path.write_bytes(b"\x00\x01")

    result = extract.extract(path, mime="audio/mpeg")

    assert result.handler == "audio"
    assert result.text == ""
    assert "faster-whisper is not installed" in result.detail
    assert "'audio' extra" in result.detail


def test_availability_is_probed_once_and_reset_clears_it(monkeypatch):
    """Ingestion asks on every file, so the probe is cached — `reset_availability` is the seam."""
    monkeypatch.setitem(sys.modules, "pypdf", None)
    extract.reset_availability()
    handler = extract.handler_for("application/pdf")
    assert handler.availability()[0] is False

    # Curing the cause is not enough: the answer is cached for the process…
    monkeypatch.setitem(sys.modules, "pypdf", types.ModuleType("pypdf"))
    assert handler.availability()[0] is False

    # …until the seam drops it.
    extract.reset_availability()
    assert handler.availability() == (True, None)


def test_availability_reports_every_handler_in_registry_order():
    statuses = extract.availability()

    assert [status.name for status in statuses] == [handler.name for handler in extract.REGISTRY]
    assert [status.name for status in statuses] == ["text", "html", "pdf", "image", "audio"]
    for status in statuses:
        assert status.mimes
        # An unavailable handler always explains itself; an available one has
        # nothing to explain.
        assert (status.detail is None) is status.available


# ── Nothing escapes ──────────────────────────────────────────────────────────


def test_a_handler_that_raises_is_reported_not_propagated(tmp_path, monkeypatch):
    def boom(self, source, *, mime):
        raise ValueError("the parser gave up")

    monkeypatch.setattr(extract.TextHandler, "extract", boom)
    path = tmp_path / "note.txt"
    path.write_text("body", encoding="utf-8")

    result = extract.extract(path, mime="text/plain")

    assert result.handler == "text"
    assert result.text == ""
    assert "ValueError" in result.detail
    assert "the parser gave up" in result.detail


def test_a_missing_file_is_reported_not_raised(tmp_path):
    result = extract.extract(tmp_path / "gone.txt", mime="text/plain")

    assert result.handler == "text"
    assert result.text == ""
    assert "FileNotFoundError" in result.detail


def test_a_handlers_explanation_is_bounded(tmp_path, monkeypatch):
    """`detail` reaches an event payload and a DB row, so a parser that quotes
    the whole offending document must not write an unbounded one."""

    def boom(self, source, *, mime):
        raise ValueError("x" * 10_000)

    monkeypatch.setattr(extract.TextHandler, "extract", boom)
    path = tmp_path / "note.txt"
    path.write_text("body", encoding="utf-8")

    result = extract.extract(path, mime="text/plain")

    assert len(result.detail) <= extract.MAX_DETAIL_CHARS + 2
    assert result.detail.endswith("…")


# ── Declared character sets ──────────────────────────────────────────────────


def test_html_honours_a_declared_charset(tmp_path):
    """A latin-1 page decoded as UTF-8 loses exactly its accents — silent
    corruption of the text the rest of the pipeline then indexes."""
    path = tmp_path / "page.html"
    path.write_bytes("<p>Comté de Franche-Comté</p>".encode("latin-1"))

    result = extract.extract(path, mime="text/html; charset=latin-1")

    assert result.text == "Comté de Franche-Comté"


def test_an_unknown_declared_charset_falls_back_rather_than_raising(tmp_path):
    """A server may declare a codec Python has never heard of; that is not a
    reason to refuse the document."""
    path = tmp_path / "page.html"
    path.write_text("<p>plain</p>", encoding="utf-8")

    result = extract.extract(path, mime="text/html; charset=x-nonesuch-9000")

    assert result.text == "plain"


def test_html_without_a_declared_charset_reads_as_utf8(tmp_path):
    path = tmp_path / "page.html"
    path.write_text("<p>Comté</p>", encoding="utf-8")

    assert extract.extract(path, mime="text/html").text == "Comté"


# ── Models are never fetched implicitly ──────────────────────────────────────


def test_audio_holds_to_the_local_cache_unless_the_download_flag_is_set(tmp_path, monkeypatch):
    """The posture `nodum.embeddings` already takes: ingesting an .mp3 must not
    silently pull hundreds of megabytes off the network."""
    seen: dict[str, object] = {}

    class _FakeModel:
        def __init__(self, name, **kwargs):
            seen.update(kwargs, name=name)

        def transcribe(self, source):
            return ([], None)

    module = types.ModuleType("faster_whisper")
    module.WhisperModel = _FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    path = tmp_path / "clip.mp3"
    path.write_bytes(b"\x00")

    monkeypatch.delenv(extract.ENV_AUDIO_DOWNLOAD_VAR, raising=False)
    extract.AudioHandler().extract(path, mime="audio/mpeg")
    assert seen["local_files_only"] is True

    monkeypatch.setenv(extract.ENV_AUDIO_DOWNLOAD_VAR, "1")
    extract.AudioHandler().extract(path, mime="audio/mpeg")
    assert seen["local_files_only"] is False


def test_the_audio_model_name_is_overridable(tmp_path, monkeypatch):
    seen: dict[str, object] = {}

    class _FakeModel:
        def __init__(self, name, **kwargs):
            seen["name"] = name

        def transcribe(self, source):
            return ([], None)

    module = types.ModuleType("faster_whisper")
    module.WhisperModel = _FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    monkeypatch.setenv(extract.ENV_AUDIO_MODEL_VAR, "small")
    path = tmp_path / "clip.mp3"
    path.write_bytes(b"\x00")

    extract.AudioHandler().extract(path, mime="audio/mpeg")

    assert seen["name"] == "small"


@requires_pypdf
def test_a_corrupt_pdf_does_not_take_the_pipeline_down(tmp_path):
    """Ingestion still has to register the asset and say plainly that nothing came out."""
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4\nthis is not a PDF body at all\n%%EOF\n")

    result = extract.extract(path, mime="application/pdf")

    assert result.handler == "pdf"
    assert result.text == ""
    assert result.detail is not None


# ── The cap ──────────────────────────────────────────────────────────────────


def test_text_above_the_cap_is_truncated_and_says_so(tmp_path, monkeypatch):
    """One pathological file must not make an unbounded row — and never silently."""
    monkeypatch.setattr(extract, "MAX_TEXT_CHARS", 32)
    path = tmp_path / "long.txt"
    path.write_text("x" * 100, encoding="utf-8")

    result = extract.extract(path, mime="text/plain")

    assert len(result.text) == 32
    assert "truncated" in result.detail
    assert "100" in result.detail


@requires_pypdf
def test_pdf_pages_stop_at_the_cap_rather_than_running_past_it(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, "MAX_TEXT_CHARS", 20)
    path = tmp_path / "book.pdf"
    path.write_bytes(_minimal_pdf(["A" * 15, "B" * 15, "C" * 15]))

    result = extract.extract(path, mime="application/pdf")

    assert len(result.text) <= 20
    assert len(result.pages) < 3
    # Pages and text stay consistent: no page carries text the cap dropped.
    assert extract.PAGE_SEPARATOR.join(result.pages) == result.text
    assert "cap" in result.detail


# ── The pdf handler on a real PDF ────────────────────────────────────────────


@requires_pypdf
def test_pdf_returns_per_page_text_and_a_joined_body(tmp_path):
    path = tmp_path / "paper.pdf"
    path.write_bytes(_minimal_pdf(["Photosynthesis converts sunlight", "Chlorophyll absorbs it"]))

    result = extract.extract(path, mime="application/pdf")

    assert result.handler == "pdf"
    assert result.detail is None
    assert len(result.pages) == 2
    assert "Photosynthesis converts sunlight" in result.pages[0]
    assert "Chlorophyll absorbs it" in result.pages[1]
    assert result.text == extract.PAGE_SEPARATOR.join(result.pages)


@requires_pypdf
def test_a_pdf_with_no_embedded_text_points_at_ocr(tmp_path):
    """A scanned PDF is images: "empty" would be a lie, so the detail names the cure."""
    path = tmp_path / "scanned.pdf"
    path.write_bytes(_minimal_pdf(["", ""]))

    result = extract.extract(path, mime="application/pdf")

    assert result.handler == "pdf"
    assert result.text == ""
    assert result.pages == ["", ""]  # page numbering survives an empty page
    assert "OCR" in result.detail
