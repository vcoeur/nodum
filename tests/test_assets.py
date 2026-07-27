"""Asset registration and rendition tests (design §5.5/§5.7).

Originals and renditions both live in the one database file. Renditions are
derived WebP images keyed by ``sha256(asset_hash + ':' + profile)``: lazily
generated, stored, evictable, and never upscaled. Tests generate their images
with Pillow and their PDFs byte by byte (:func:`_make_pdf`) — no network, no
fixtures on disk, and the PDF path is exercised against a file this suite
wrote rather than one PDFium wrote for itself.
"""

from __future__ import annotations

import codecs
import hashlib
import io
import random
import sqlite3
import sys
import zipfile
from importlib.util import find_spec
from pathlib import Path

import pytest
from helpers import agent, owner, seed_space
from PIL import Image

from nodum import assets, db, extract, service
from nodum.assets import AssetNotFound, ImageTooLarge, UnsupportedRendition


def _decode(rendition, path=None):
    """Open a rendition's stored WebP bytes as a Pillow image."""
    return Image.open(io.BytesIO(assets.read_rendition_bytes(rendition, path=path)))


def _make_image(path, size=(2000, 1000), mode="RGB", noise=False):
    """Write a deterministic test image (noise compresses badly on purpose)."""
    image = Image.new(mode, size)
    if noise:
        rng = random.Random(0)
        pixels = [
            (rng.randrange(256), rng.randrange(256), rng.randrange(256))
            for _ in range(size[0] * size[1])
        ]
        image.putdata(pixels)
    image.save(path)
    return path


def _register_image(fresh_db, tmp_path, name="photo.png", **kwargs):
    """Register a generated image and return its AssetOut."""
    source = _make_image(tmp_path / name, **kwargs)
    return assets.register_asset(source)


def _make_pdf(path, *, pages=("alpha", "beta"), width=612, height=792):
    """Write a minimal, valid multi-page PDF with one word drawn on each page.

    Hand-assembled rather than produced by pypdfium2: the renderer under test
    should be pointed at a file it did not write, and building the xref table
    here keeps the fixture free of the very dependency whose absence one of
    these tests simulates. Defaults are US Letter in PDF canvas units (1/72
    inch), so a page renders 1224×1584 at `assets.PAGE_DPI`.
    """
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # patched once the page tree's object number is known
    page_tree = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    kids = []
    for text in pages:
        drawing = f"BT /F1 36 Tf 72 {height - 100} Td ({text}) Tj ET".encode()
        content = add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(drawing), drawing))
        kids.append(
            add(
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d]"
                b" /Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
                % (page_tree, width, height, font, content)
            )
        )
    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % page_tree
    objects[page_tree - 1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % kid for kid in kids),
        len(kids),
    )

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        catalog,
        xref_at,
    )
    Path(path).write_bytes(bytes(out))
    return Path(path)


def _register_pdf(tmp_path, name="paper.pdf", **kwargs):
    """Register a generated PDF and return its AssetOut."""
    return assets.register_asset(_make_pdf(tmp_path / name, **kwargs))


def _webp_at(image, quality):
    """Encode a prepared image as WebP at one quality, exactly as the encoder does."""
    buffer = io.BytesIO()
    image.save(buffer, "WEBP", quality=quality)
    return buffer.getvalue()


def _prepared(db_path, asset_hash, profile):
    """Return the downscaled image the encoder is handed for a profile."""
    conn = db.connect(db_path)
    try:
        with assets.open_original(conn, asset_hash) as original:
            return assets._prepare_image(original, assets.PROFILES[profile])
    finally:
        conn.close()


# ── Registration (a metadata row + a blob + a sha256) ─────────────────────────


def test_register_streams_bytes_into_the_database(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path)
    original = (tmp_path / "photo.png").read_bytes()
    assert asset.hash == hashlib.sha256(original).hexdigest()
    assert asset.mime == "image/png"
    assert asset.original_name == "photo.png"
    assert asset.size_bytes == len(original)

    conn = db.connect(fresh_db)
    try:
        stored = conn.execute(
            "SELECT data FROM asset_blobs WHERE hash = ?", (asset.hash,)
        ).fetchone()["data"]
    finally:
        conn.close()
    assert stored == original


def test_register_writes_nothing_beside_the_database(fresh_db, tmp_path):
    """The single-file promise: no asset directory, no rendition cache on disk."""
    asset = _register_image(fresh_db, tmp_path)
    assets.get_rendition(asset.hash, profile="thumb", principal=owner())
    beside = {child.name for child in fresh_db.parent.iterdir()}
    assert not {name for name in beside if name in ("assets", "renditions")}


def test_zero_byte_asset_registers(fresh_db, tmp_path):
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    asset = assets.register_asset(empty)
    assert asset.size_bytes == 0
    assert assets.get_asset(asset.hash, principal=owner()).hash == asset.hash


def test_register_dedups_identical_content(fresh_db, tmp_path):
    first = _register_image(fresh_db, tmp_path)
    second = assets.register_asset(tmp_path / "photo.png", name="renamed.jpg")
    assert second.hash == first.hash
    assert second.original_name == "photo.png"  # the existing row wins
    # And so does its MIME. The second name guesses `image/jpeg`, so this is the
    # dedup hit refusing to re-derive a type from a *name* — while still holding
    # the repair below, which only a signature from another family triggers.
    assert (first.mime, second.mime) == ("image/png", "image/png")
    assert len(assets.list_assets(principal=owner())) == 1


def test_a_dedup_hit_repairs_a_stored_mime_a_signature_contradicts(fresh_db, tmp_path):
    """A pre-upgrade row must not poison every later reader of it (review F10).

    Registration returns on the sha256 hit, so a wrong type recorded under an
    older rule was permanent: these PDF bytes, first registered as ``scan.txt``
    when the name decided the MIME, stayed ``text/plain`` when the same bytes
    were ingested as ``scan.pdf`` — admitted by the policy, and then handed to no
    handler, with no page blocks and a refused ``page:1``, while the row reported
    a successful ingest.
    """
    pdf = _make_pdf(tmp_path / "scan.pdf")
    stale = tmp_path / "scan.txt"
    stale.write_bytes(pdf.read_bytes())
    conn = db.connect(fresh_db)
    try:
        # The pre-upgrade state, written the way the old rule would have.
        assets.register_asset(stale)
        conn.execute("UPDATE assets SET mime = 'text/plain'")
        conn.commit()
    finally:
        conn.close()

    repaired = assets.register_asset(pdf)

    assert repaired.mime == "application/pdf"
    assert assets.get_asset(repaired.hash, principal=owner()).mime == "application/pdf"
    # And the repair is what makes the rest of the pipeline reachable again.
    assert extract.handler_for(repaired.mime) is not None
    if find_spec("pypdfium2") is not None:
        page = assets.get_rendition(repaired.hash, profile="page:1", principal=owner())
        assert page.width > 0


def test_a_dedup_hit_keeps_a_more_specific_stored_mime(fresh_db, tmp_path):
    """The repair is across families only: `text/markdown` is not contradicted by
    the text heuristic, it is a better answer than it."""
    notes = tmp_path / "notes.md"
    notes.write_bytes(b"# heading\n")
    first = assets.register_asset(notes)
    same_bytes = tmp_path / "notes.txt"
    same_bytes.write_bytes(b"# heading\n")

    assert first.mime == "text/markdown"
    assert assets.register_asset(same_bytes).mime == "text/markdown"


def test_register_distinct_content_gets_distinct_hashes(fresh_db, tmp_path):
    one = _register_image(fresh_db, tmp_path, name="a.png")
    other = _register_image(fresh_db, tmp_path, name="b.png", size=(10, 10))
    assert one.hash != other.hash
    assert len(assets.list_assets(principal=owner())) == 2


# ── Sniffing the bytes, and the MIME that gets stored (note 01 D2/D3) ────────


#: One hand-written sample per type identified by a *signature*. Signature bytes
#: are enough — the sniffer reads a window and never decodes — and the coverage
#: assertion below keeps this table plus :data:`PILLOW_RASTERS` level with
#: ``assets.RECOGNISED_MIMES``. The names are the names such a file would arrive
#: under and are deliberately wrong for the bytes; nothing reads them here.
SNIFF_SAMPLES: tuple[tuple[str, bytes, str], ...] = (
    ("notes.txt", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", "image/png"),
    ("notes.txt", b"\xff\xd8\xff\xe0\x00\x10JFIF", "image/jpeg"),
    ("notes.txt", b"GIF87a\x01\x00\x01\x00", "image/gif"),
    ("notes.txt", b"GIF89a\x01\x00\x01\x00", "image/gif"),
    ("notes.txt", b"RIFF\x1a\x00\x00\x00WEBPVP8L", "image/webp"),
    ("notes.txt", b"BM\x8a\x00\x00\x00\x00\x00", "image/bmp"),
    ("notes.txt", b"II*\x00\x08\x00\x00\x00", "image/tiff"),
    ("notes.txt", b"MM\x00*\x00\x00\x00\x08", "image/tiff"),
    ("cover.png", b"%PDF-1.7\n1 0 obj\n", "application/pdf"),
    ("cover.png", b"ID3\x04\x00\x00\x00\x00\x00#TSSE", "audio/mpeg"),
    ("cover.png", b"RIFF$\x00\x00\x00WAVEfmt ", "audio/wav"),
    ("cover.png", b"OggS\x00\x02\x00\x00\x00\x00", "audio/ogg"),
    ("cover.png", b'fLaC\x00\x00\x00"', "audio/flac"),
    # The two audio-only ISO-BMFF brands, and nothing wider (see the video row
    # in the refusal table below — an `mp4` *prefix* match claimed video).
    ("cover.png", b"\x00\x00\x00 ftypM4A \x00\x00\x02\x00", "audio/mp4"),
    ("cover.png", b"\x00\x00\x00 ftypM4B \x00\x00\x02\x00", "audio/mp4"),
    ("archive.zip", "les cimes enneigées\n".encode(), "text/plain"),
)

#: Every raster this Pillow build reads, as **Pillow writes it** — the claim
#: being tested is "the admitted set is what this install can act on", so
#: hand-writing the headers would test the table against itself. The four after
#: the classic six are review F8's: each carries NULs in its header, so the text
#: heuristic could never name it, and each renders a thumbnail happily.
PILLOW_RASTERS: tuple[tuple[str, dict, str], ...] = (
    ("photo.png", {}, "image/png"),
    ("photo.jpeg", {}, "image/jpeg"),
    ("photo.gif", {}, "image/gif"),
    ("photo.webp", {}, "image/webp"),
    ("photo.bmp", {}, "image/bmp"),
    ("photo.tif", {}, "image/tiff"),
    ("photo.jp2", {}, "image/jp2"),
    ("codestream.j2k", {}, "image/jp2"),
    ("photo.avif", {}, "image/avif"),
    ("photo.ico", {}, "image/x-icon"),
    ("scan.tif", {"big_tiff": True}, "image/tiff"),
)

#: What ``mimetypes`` guesses for a ``.docx`` — long enough to be worth a name.
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.mark.parametrize(("name", "payload", "expected"), SNIFF_SAMPLES)
def test_every_signature_type_is_named_from_its_bytes(tmp_path, name, payload, expected):
    """The bytes name the type, and the filename is not consulted at all.

    `sniff_mime` takes a path and never looks at its name — the rows above are
    named the way such a file would arrive, but what they assert is only that a
    leading signature is enough. Whether a *name* can outrank the bytes is the
    stored-MIME table's question, further down.
    """
    source = tmp_path / name
    source.write_bytes(payload)
    assert assets.sniff_mime(source) == expected


@pytest.mark.parametrize(("name", "options", "expected"), PILLOW_RASTERS)
def test_every_raster_this_pillow_reads_is_named_and_renders(
    fresh_db, tmp_path, name, options, expected
):
    """The recognised set is what this install can act on — proved on both halves.

    JPEG 2000, AVIF, ICO and BigTIFF were refused at the door while
    `register_asset` + `get_rendition` on the very same bytes produced a
    thumbnail, so the policy was refusing its own capability (review F8).
    """
    source = tmp_path / name
    Image.new("RGB", (64, 48), "red").save(source, **options)

    assert assets.sniff_mime(source) == expected
    assert expected in assets.RECOGNISED_IMAGE_MIMES
    asset = assets.register_asset(source)
    thumb = assets.get_rendition(asset.hash, profile="thumb", principal=owner())
    assert (thumb.mime, thumb.width > 0, thumb.height > 0) == (assets.RENDITION_MIME, True, True)


def test_the_sniff_samples_cover_the_whole_recognised_vocabulary():
    """`http_api` derives its widest admitted set from this vocabulary, so a type
    added to the sniffer with no sample here would go untested by construction."""
    named = {expected for _name, _payload, expected in SNIFF_SAMPLES}
    named |= {expected for _name, _options, expected in PILLOW_RASTERS}
    assert named == assets.RECOGNISED_MIMES
    assert {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/bmp",
        "image/tiff",
        "image/jp2",
        "image/avif",
        "image/x-icon",
    } == assets.RECOGNISED_IMAGE_MIMES


def test_every_recognised_type_has_an_extraction_handler():
    """The stated justification for the vocabulary, asserted rather than asserted-in-prose.

    `RECOGNISED_MIMES` is "what this system can act on", derived from the
    rendition path and `nodum.extract`'s registry. If a member reached neither,
    the network would admit bytes nothing downstream claims.
    """
    unclaimed = sorted(
        mime for mime in assets.RECOGNISED_MIMES if extract.handler_for(mime) is None
    )
    assert unclaimed == []


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        # A renamed executable: the hole the one-sniffer policy closes.
        ("innocent.pdf", b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00"),
        # A `.docx` is a zip, which no handler claims — the deliberate cost.
        ("report.docx", b"PK\x03\x04\x14\x00\x06\x00\x08\x00\x00\x00"),
        # ISO base media with a *video* brand: `nodum.extract` claims no
        # `video/*` family, so naming it would admit bytes nothing can read.
        ("clip.m4a", b"\x00\x00\x00 ftypisom\x00\x00\x02\x00"),
        # And the brand that made the prefix match wrong: ffmpeg writes `mp42`
        # for ordinary *video*, so `brand.startswith(b"mp4")` claimed video as
        # `audio/mp4` and gave two videos opposite answers (review F7).
        ("clip.mp4", b"\x00\x00\x00 ftypmp42\x00\x00\x02\x00mdat"),
        # RIFF is a container; neither of the two forms this system reads.
        ("track.wav", b"RIFF$\x00\x00\x00AVI LIST"),
        # NUL-free, but a bell is not prose.
        ("log.txt", b"progress: \x07\x07 done"),
    ],
)
def test_bytes_this_system_cannot_act_on_sniff_to_nothing(tmp_path, name, payload):
    """`None` is the answer a network surface refuses on, whatever the name claims."""
    source = tmp_path / name
    source.write_bytes(payload)
    assert assets.sniff_mime(source) is None


def test_text_is_decided_by_gits_rule_over_a_window_at_each_end(tmp_path):
    """git's `buffer_is_binary`: a NUL in a window means binary.

    The windows are bounded, so a NUL between them is never consulted — the cost
    the design states rather than hides. An empty file is **not** text: it is no
    evidence at all, and every test in the heuristic passes vacuously over zero
    bytes, which is how a zero-byte `.exe` was admitted and given a whole
    subgraph (review F5).
    """
    prose = tmp_path / "prose"
    prose.write_bytes(b"tabs\tlines\nreturns\r\nform\x0cfeed\x0bvertical\x1b[0mescape")
    assert assets.sniff_mime(prose) == assets.TEXT_MIME

    inside = tmp_path / "inside"
    inside.write_bytes(b"a" * 100 + b"\x00" + b"a" * 100)
    assert assets.sniff_mime(inside) is None

    between = tmp_path / "between"
    between.write_bytes(b"a" * assets._SNIFF_BYTES + b"\x00" + b"a" * assets._SNIFF_BYTES)
    assert assets.sniff_mime(between) == assets.TEXT_MIME

    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    assert assets.sniff_mime(empty) is None


def test_the_text_window_is_read_from_both_ends(tmp_path):
    """A binary file with a long ASCII prefix is admitted by a head-only test.

    4096 spaces in front of a zip is still a valid zip — its central directory
    is at the *end*, which is exactly where the second window looks (review F4).
    """
    body = io.BytesIO()
    with zipfile.ZipFile(body, "w") as archive:
        archive.writestr("word/document.xml", "<w:document/>")
    padded = b"A" * assets._SNIFF_BYTES + body.getvalue()
    source = tmp_path / "report.docx"
    source.write_bytes(padded)

    assert zipfile.is_zipfile(io.BytesIO(padded)), "the padded file is still a real zip"
    assert assets.sniff_mime(source) is None


@pytest.mark.parametrize(
    ("bom", "encoding"),
    [
        (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
        (codecs.BOM_UTF32_LE, "utf-32-le"),
        (codecs.BOM_UTF32_BE, "utf-32-be"),
    ],
)
def test_a_utf16_or_utf32_bom_exempts_a_file_from_the_nul_rule_and_nothing_else(
    tmp_path, bom, encoding
):
    """Those encodings spell ASCII with NUL padding, so the NUL rule alone would
    call every one of them binary — but the exemption is from that rule only.

    The window is decoded in the marked encoding and still has to be free of
    control characters, at both ends. A BOM used to short-circuit the whole
    heuristic, so `BOM + <a program>` was admitted as text (review F4).
    """
    source = tmp_path / "cimes.txt"
    source.write_bytes(bom + "les cimes enneigées".encode(encoding))
    assert assets.sniff_mime(source) == assets.TEXT_MIME

    program = tmp_path / "innocent.pdf"
    program.write_bytes(bom + b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 200)
    assert assets.sniff_mime(program) is None

    body = io.BytesIO()
    with zipfile.ZipFile(body, "w") as archive:
        archive.writestr("word/document.xml", "<w:document/>")
    padded = tmp_path / "padded.docx"
    padded.write_bytes(bom + ("a" * 3000).encode(encoding) + body.getvalue())
    assert assets.sniff_mime(padded) is None


def test_a_utf8_bom_proves_nothing_and_no_longer_admits_a_binary(tmp_path):
    """UTF-8 text passes the ordinary byte test unaided, so honouring its mark
    bought only a bypass: three bytes in front of an `.exe` made it text, and the
    capability route admitted it, stored it, and wrote a whole subgraph (F4)."""
    prose = tmp_path / "cimes.txt"
    prose.write_bytes(codecs.BOM_UTF8 + "les cimes enneigées\n".encode())
    assert assets.sniff_mime(prose) == assets.TEXT_MIME

    for payload in (
        codecs.BOM_UTF8 + b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00program",
        codecs.BOM_UTF8 + b"\x00" * assets._SNIFF_BYTES,
    ):
        program = tmp_path / "innocent.pdf"
        program.write_bytes(payload)
        assert assets.sniff_mime(program) is None


@pytest.mark.parametrize(
    ("name", "payload", "expected"),
    [
        # A *signature* wins when the name names another family: the stored MIME
        # is what `page:<n>` and extraction dispatch on, so a PDF delivered under
        # any of these names has to land as one.
        ("scan.txt", b"%PDF-1.7\n1 0 obj\n", "application/pdf"),
        ("scan", b"%PDF-1.7\n1 0 obj\n", "application/pdf"),
        ("scan.bin", b"%PDF-1.7\n1 0 obj\n", "application/pdf"),
        ("cover.png", b"ID3\x04\x00\x00\x00\x00\x00#TSSE", "audio/mpeg"),
        # The name keeps its specificity inside one family: flattening these to
        # the sniff's text/plain would send each to the wrong handler.
        ("notes.md", b"# heading\n", "text/markdown"),
        ("page.html", b"<p>markup</p>", "text/html"),
        ("basin.csv", b"a,b\n1,2\n", "text/csv"),
        # The text heuristic is weak evidence and may only *fill in*, so these
        # keep their own names although all three sniff as text (review F3).
        # `application/json` and `application/xhtml+xml` need no special-case
        # list any more; SVG is a raster's name over bytes Pillow cannot render.
        ("data.json", b'{"basin": 1}', "application/json"),
        ("page.xhtml", b"<html/>", "application/xhtml+xml"),
        ("logo.svg", b'<svg xmlns="http://www.w3.org/2000/svg"/>', "image/svg+xml"),
        # Unrecognised bytes keep exactly today's behaviour: the name's guess,
        # then the octet-stream fallback.
        ("report.docx", b"PK\x03\x04\x14\x00\x06\x00\x08\x00\x00\x00", DOCX_MIME),
        ("mystery.bin", b"\x00\x01\x02\x03", assets.FALLBACK_MIME),
        ("mystery", b"\x00\x01\x02\x03", assets.FALLBACK_MIME),
    ],
)
def test_the_stored_mime_prefers_a_signature_over_a_name_from_another_family(
    fresh_db, tmp_path, name, payload, expected
):
    """Note 01 D3 as revised by review F3, on new registrations only."""
    source = tmp_path / name
    source.write_bytes(payload)
    assert assets.register_asset(source).mime == expected


def test_a_shifted_pdf_header_keeps_the_name_the_bytes_cannot_provide(fresh_db, tmp_path):
    """The text heuristic is a window guess, and it used to outrank a real name.

    A PDF whose `%PDF-` sits one byte in is read by both `pypdf` and `pypdfium2`
    and matches no signature, so it sniffs as text. Letting that overrule `.pdf`
    cost the document its handler and its page rasters and put raw PDF bytes
    into `assets.extracted_text` — and therefore into the FTS index (review F3).

    Note which path this exercises: `_make_pdf` hand-assembles an uncompressed
    PDF, so its bytes are NUL-free from end to end and the *text* branch answers.
    A PDF carrying a compressed stream does not sniff as text at all — see
    `test_a_displaced_pdf_header_is_definite_when_the_bytes_are_not_text`, which
    is the case this fixture cannot reach.
    """
    shifted = tmp_path / "sample.pdf"
    shifted.write_bytes(b"\n" + _make_pdf(tmp_path / "real.pdf").read_bytes())

    assert assets.sniff_mime(shifted) == assets.TEXT_MIME
    asset = assets.register_asset(shifted)
    assert asset.mime == "application/pdf"
    assert extract.handler_for(asset.mime).name == "pdf"


def _binary_stream_pdf(path):
    """Write a PDF whose image stream carries NUL bytes, like any real one.

    Every PDF a human actually drops — a scan, an export, anything with a font
    subset or a compressed image — has binary streams in it, and so does not
    sniff as text. The hand-assembled fixture above cannot represent that, which
    is exactly how the displaced-header refusal survived a green suite.
    """
    Image.new("RGB", (24, 18), "teal").save(path, "PDF")
    assert b"\x00" in path.read_bytes(), "fixture must carry binary stream bytes"
    return path


def test_a_displaced_pdf_header_is_definite_when_the_bytes_are_not_text(fresh_db, tmp_path):
    """A `%PDF-` marker need not lead the file, and the readers already know that.

    Found by the live end-to-end pass rather than by a test: `pypdf` and PDFium
    both scan for the header, so a real PDF behind a stray byte extracts,
    paginates and rasterises — while the sniffer called it nothing at all and the
    route refused it outright. The text test runs first, which is what keeps the
    scan safe: prose quoting the marker is text and never reaches it.
    """
    displaced = tmp_path / "scan.pdf"
    displaced.write_bytes(b"\n" + _binary_stream_pdf(tmp_path / "real.pdf").read_bytes())

    assert assets.sniff_mime(displaced) == "application/pdf"
    assert assets.register_asset(displaced).mime == "application/pdf"


def test_a_displaced_header_outranks_a_name_from_another_family(fresh_db, tmp_path):
    """Definite evidence, so it behaves like a leading signature and overrules."""
    misnamed = tmp_path / "scan.txt"
    misnamed.write_bytes(b"\n" + _binary_stream_pdf(tmp_path / "real.pdf").read_bytes())

    assert assets.register_asset(misnamed).mime == "application/pdf"


def test_prose_quoting_a_pdf_header_stays_text(fresh_db, tmp_path):
    """The ordering, stated as a test, because this repository's own docs do it.

    `docs/architecture.md` and `AGENTS.md` both quote `%PDF-`. A bounded scan
    would have made this rare; running the text test first makes it impossible.
    """
    prose = tmp_path / "architecture.md"
    prose.write_bytes(b"The sniffer matches %PDF-1.4 at the head of the window.\n" * 4)

    assert assets.sniff_mime(prose) == assets.TEXT_MIME
    assert assets.register_asset(prose).mime == "text/markdown"


def test_registration_still_refuses_nothing_on_type(fresh_db, tmp_path):
    """The sniff decides what is *recorded*, never what is *accepted*.

    `register_asset` takes no principal and the CLI's tolerance for arbitrary
    operator-owned files is deliberate: a type policy belongs to the surfaces
    that take bytes from a stranger (note 01 D2).
    """
    for index, payload in enumerate((b"MZ\x90\x00program", b"PK\x03\x04\x14\x00", b"\x00" * 32)):
        source = tmp_path / f"whatever-{index}"
        source.write_bytes(payload)
        assert assets.register_asset(source).mime == assets.FALLBACK_MIME


# ── Metadata resolution: by hash or by asset-reference node ──────────────────


def test_get_asset_by_hash_and_by_node_id(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path)
    assert assets.get_asset(asset.hash, principal=owner()).hash == asset.hash

    node = service.create_node(
        type="asset_ref",
        title="Photo",
        props={"asset_hash": asset.hash},
        principal=owner(),
    )
    assert assets.get_asset(node.id, principal=owner()).hash == asset.hash


def test_get_asset_unknown_raises(fresh_db):
    with pytest.raises(AssetNotFound):
        assets.get_asset("missing", principal=owner())


# ── Geometry: downscale to the profile cap, never upscale ────────────────────


def test_thumb_downscales_to_256_max_edge(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path, size=(2000, 1000))
    rendition = assets.get_rendition(asset.hash, profile="thumb", principal=owner())
    assert (rendition.width, rendition.height) == (256, 128)
    with _decode(rendition) as decoded:
        assert decoded.format == "WEBP"
        assert decoded.size == (256, 128)


def test_preview_downscales_to_1024_max_edge(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path, size=(2000, 1000))
    rendition = assets.get_rendition(asset.hash, profile="preview", principal=owner())
    assert (rendition.width, rendition.height) == (1024, 512)


def test_small_images_are_never_upscaled(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path, size=(100, 50))
    for profile, expected in (("thumb", (100, 50)), ("preview", (100, 50))):
        rendition = assets.get_rendition(asset.hash, profile=profile, principal=owner())
        assert (rendition.width, rendition.height) == expected


def test_rgba_alpha_survives_rendition(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path, mode="RGBA", size=(800, 800))
    rendition = assets.get_rendition(asset.hash, profile="preview", principal=owner())
    with _decode(rendition) as decoded:
        assert decoded.mode == "RGBA"


def test_thumb_is_encoded_at_the_profiles_nominal_quality(fresh_db, tmp_path):
    """`thumb` has no size target, so its q75 is the encode — not the ladder's q70.

    A WebP file records no quality factor, so the only way to pin this is to
    re-encode the same prepared image here and compare bytes.
    """
    asset = _register_image(fresh_db, tmp_path, size=(600, 300), noise=True)
    rendition = assets.get_rendition(asset.hash, profile="thumb", principal=owner())
    stored = assets.read_rendition_bytes(rendition)

    prepared = _prepared(fresh_db, asset.hash, "thumb")
    assert assets.PROFILES["thumb"].quality == 75
    assert stored == _webp_at(prepared, 75)
    assert stored != _webp_at(prepared, 70)


def test_preview_encodes_at_q80_when_it_already_fits_the_target(fresh_db, tmp_path):
    """A target profile also starts at its nominal quality; steps are the fallback."""
    asset = _register_image(fresh_db, tmp_path, size=(400, 400))
    rendition = assets.get_rendition(asset.hash, profile="preview", principal=owner())
    stored = assets.read_rendition_bytes(rendition)

    prepared = _prepared(fresh_db, asset.hash, "preview")
    assert len(stored) <= assets.PROFILES["preview"].target_bytes
    assert stored == _webp_at(prepared, 80)


def test_preview_respects_the_300kb_target(fresh_db, tmp_path):
    # Noise compresses badly: q80 WebP of this is far above 300 KB, forcing
    # the quality-stepping loop to fit the target.
    asset = _register_image(fresh_db, tmp_path, size=(1600, 1600), noise=True)
    rendition = assets.get_rendition(asset.hash, profile="preview", principal=owner())
    assert rendition.size_bytes <= 300_000
    assert (rendition.width, rendition.height) == (1024, 1024)
    # The nominal quality really was too big — the ladder is what fit it.
    assert len(_webp_at(_prepared(fresh_db, asset.hash, "preview"), 80)) > 300_000


# ── Modes WebP cannot encode directly are converted first ────────────────────


@pytest.mark.parametrize(
    ("mode", "name"),
    [("P", "palette.png"), ("L", "grayscale.png"), ("CMYK", "print.jpg")],
)
def test_non_rgb_originals_are_converted_before_encoding(fresh_db, tmp_path, mode, name):
    """Palette, grayscale and CMYK originals still render.

    Real files arrive in these modes — exported palettes, scans, print-ready
    JPEGs — and WebP takes none of them: without the conversion branch the
    rendition would fail on the first non-RGB upload.
    """
    source = tmp_path / name
    Image.new(mode, (300, 150)).save(source)
    with Image.open(source) as reopened:
        assert reopened.mode == mode  # the mode really survived the round-trip

    asset = assets.register_asset(source)
    rendition = assets.get_rendition(asset.hash, profile="thumb", principal=owner())
    assert (rendition.width, rendition.height) == (256, 128)
    with _decode(rendition) as decoded:
        assert decoded.format == "WEBP"
        assert decoded.mode == "RGB"


def test_palette_transparency_becomes_alpha(fresh_db, tmp_path):
    """A palette image with a transparent index converts to RGBA, not RGB.

    Alpha lives in `info["transparency"]` for `P` mode, not in the bands, so
    only checking the bands would silently flatten transparent PNGs.
    """
    source = tmp_path / "transparent.png"
    image = Image.new("P", (200, 100), 1)
    image.putpalette([255, 0, 0] * 256)
    image.paste(0, (0, 0, 100, 100))  # half the image on the transparent index
    image.save(source, transparency=0)

    asset = assets.register_asset(source)
    with _decode(assets.get_rendition(asset.hash, profile="thumb", principal=owner())) as decoded:
        assert decoded.mode == "RGBA"
        assert decoded.convert("RGBA").getpixel((10, 10))[3] == 0  # still transparent


# ── Addressing, caching, eviction ─────────────────────────────────────────────


def test_rendition_id_is_sha256_of_hash_and_profile(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path)
    rendition = assets.get_rendition(asset.hash, profile="thumb", principal=owner())
    expected = hashlib.sha256(f"{asset.hash}:thumb".encode()).hexdigest()
    assert rendition.id == expected
    assert rendition.mime == "image/webp"


def test_lazy_generation_then_cache_hit(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path)
    generated = assets.get_rendition(asset.hash, profile="preview", principal=owner())
    assert generated.cached is False
    hit = assets.get_rendition(asset.hash, profile="preview", principal=owner())
    assert hit.cached is True
    assert hit.id == generated.id


def test_include_data_embeds_base64_webp(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path, size=(10, 10))
    rendition = assets.get_rendition(
        asset.hash, profile="thumb", include_data=True, principal=owner()
    )
    raw = assets.read_rendition_bytes(rendition)
    assert raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"


def test_metadata_only_fetch_reads_bytes_from_the_database(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path, size=(10, 10))
    rendition = assets.get_rendition(asset.hash, profile="thumb", principal=owner())
    assert rendition.data_base64 is None
    raw = assets.read_rendition_bytes(rendition)
    assert raw[:4] == b"RIFF" and len(raw) == rendition.size_bytes


def test_purge_evicts_stored_renditions(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path)
    assets.get_rendition(asset.hash, profile="thumb", principal=owner())
    assets.get_rendition(asset.hash, profile="preview", principal=owner())

    result = assets.purge_renditions()
    assert result.purged == 2
    assert result.bytes_freed > 0

    conn = db.connect(fresh_db)
    try:
        assert conn.execute("SELECT count(*) AS n FROM renditions").fetchone()["n"] == 0
    finally:
        conn.close()
    # Derived data regenerates on the next request.
    assert assets.get_rendition(asset.hash, profile="thumb", principal=owner()).cached is False


def test_purge_scoped_to_one_asset(fresh_db, tmp_path):
    one = _register_image(fresh_db, tmp_path, name="a.png")
    other = _register_image(fresh_db, tmp_path, name="b.png", size=(10, 10))
    assets.get_rendition(one.hash, profile="thumb", principal=owner())
    assets.get_rendition(other.hash, profile="thumb", principal=owner())

    result = assets.purge_renditions(asset_hash=one.hash)
    assert result.purged == 1
    assert assets.get_rendition(other.hash, profile="thumb", principal=owner()).cached is True


# ── Calling convention ────────────────────────────────────────────────────────


def test_options_including_the_db_path_are_keyword_only(fresh_db, tmp_path):
    """Every public function follows the service/search convention.

    `path` used to be positional here alone, so `get_asset(x, y)` quietly read
    `y` as a database path where `service.get_node(x, y, principal=owner())` is a TypeError.
    """
    asset = _register_image(fresh_db, tmp_path)
    rendition = assets.get_rendition(asset.hash, profile="thumb", principal=owner())
    for call in (
        lambda: assets.register_asset(tmp_path / "photo.png", "renamed.png"),
        lambda: assets.get_asset(asset.hash, "not-a-database", principal=owner()),
        lambda: assets.list_assets(fresh_db, principal=owner()),
        lambda: assets.get_rendition(asset.hash, "thumb", principal=owner()),
        lambda: assets.read_rendition_bytes(rendition, fresh_db),
        lambda: assets.purge_renditions(asset.hash),
        lambda: assets.copy_rendition(rendition, tmp_path / "out.webp", fresh_db),
    ):
        with pytest.raises(TypeError, match="positional"):
            call()


# ── Access (interim, until Phase 4 gives assets a space) ──────────────────────


def _describe(asset, space=None, principal=None):
    """Create the `asset_ref` node that makes an asset reachable in a space."""
    return service.create_node(
        type="asset_ref",
        title=asset.original_name or asset.hash[:8],
        space=space,
        props={"asset_hash": asset.hash},
        principal=principal or owner(),
    )


def test_an_asset_nobody_describes_is_invisible_to_every_agent(fresh_db, tmp_path):
    """Bytes with no describing node are a human's business until ingestion runs."""
    asset = _register_image(fresh_db, tmp_path)
    reader = agent("reader", grants={"main": "read"})

    assert assets.list_assets(principal=reader) == []
    with pytest.raises(AssetNotFound):
        assets.get_asset(asset.hash, principal=reader)
    with pytest.raises(AssetNotFound):
        assets.get_rendition(asset.hash, profile="thumb", principal=reader)
    # The human still sees it — the bytes are registered, just undescribed.
    assert assets.get_asset(asset.hash, principal=owner()).hash == asset.hash


def test_a_described_asset_is_readable_in_that_nodes_space(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path)
    _describe(asset)
    reader = agent("reader", grants={"main": "read"})

    assert assets.get_asset(asset.hash, principal=reader).hash == asset.hash
    assert [row.hash for row in assets.list_assets(principal=reader)] == [asset.hash]
    assert assets.get_rendition(asset.hash, profile="thumb", principal=reader).asset_hash == (
        asset.hash
    )


def test_a_description_in_another_space_does_not_reach(fresh_db, tmp_path):
    """The describing node carries the space, so it carries the isolation too."""
    asset = _register_image(fresh_db, tmp_path)
    seed_space("b")
    _describe(asset, space="b")
    outsider = agent("outsider", grants={"main": "read"})
    insider = agent("insider", grants={"b": "read"})

    assert assets.list_assets(principal=outsider) == []
    with pytest.raises(AssetNotFound):
        assets.get_asset(asset.hash, principal=outsider)
    assert assets.get_asset(asset.hash, principal=insider).hash == asset.hash


def test_archiving_the_last_description_takes_the_asset_out_of_reach(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path)
    node = _describe(asset)
    reader = agent("reader", grants={"main": "read"})
    assert assets.get_asset(asset.hash, principal=reader).hash == asset.hash

    service.transition(node.id, "archive", principal=owner())

    with pytest.raises(AssetNotFound):
        assets.get_asset(asset.hash, principal=reader)


def test_a_proposed_description_grants_reach_like_any_other_readable_node(fresh_db, tmp_path):
    """A proposed node is readable everywhere else in this system — search
    filters it out at query time, reads do not hide it — so an asset must not
    be the one thing with a stricter rule than the node describing it. It is
    also what lets a `suggest` agent re-read the bytes it just ingested."""
    asset = _register_image(fresh_db, tmp_path)
    proposer = agent("proposer", grants={"meta": "read", "main": "suggest"})
    node = _describe(asset, principal=proposer)
    assert node.state == "proposed"

    assert [row.hash for row in assets.list_assets(principal=proposer)] == [asset.hash]
    assert assets.get_asset(asset.hash, principal=proposer).hash == asset.hash

    # Accepting changes nothing about reach — it was already reachable.
    service.transition(node.id, "accept", principal=owner())
    assert [row.hash for row in assets.list_assets(principal=proposer)] == [asset.hash]


def test_an_archived_description_stops_granting_reach(fresh_db, tmp_path):
    """Archived is the state that revokes reach, and the only one."""
    asset = _register_image(fresh_db, tmp_path)
    reader = agent("archiver", grants={"meta": "read", "main": "edit"})
    node = _describe(asset, principal=reader)
    assert [row.hash for row in assets.list_assets(principal=reader)] == [asset.hash]

    service.transition(node.id, "archive", principal=owner())

    assert assets.list_assets(principal=reader) == []


def test_an_asset_ref_node_in_an_unreadable_space_does_not_resolve(fresh_db, tmp_path):
    """The node-id handle must not become an existence oracle either."""
    asset = _register_image(fresh_db, tmp_path)
    seed_space("b")
    node = service.create_node(
        type="asset_ref",
        title="Scan",
        space="b",
        props={"asset_hash": asset.hash},
        principal=owner(),
    )
    reader = agent("reader", grants={"main": "read"})

    with pytest.raises(AssetNotFound):
        assets.get_asset(node.id, principal=reader)
    # The same handle resolves for a principal that can read space b.
    granted = agent("granted", grants={"b": "read"})
    assert assets.get_asset(node.id, principal=granted).hash == asset.hash


# ── Clean rejection ───────────────────────────────────────────────────────────


def test_non_image_assets_are_rejected(fresh_db, tmp_path):
    text_file = tmp_path / "notes.txt"
    text_file.write_text("plain text, not an image")
    asset = assets.register_asset(text_file)
    with pytest.raises(UnsupportedRendition, match="only supported for image assets"):
        assets.get_rendition(asset.hash, principal=owner())


def test_unreadable_image_bytes_are_rejected(fresh_db, tmp_path):
    """Two refusals, and which one a file gets follows the strength of the evidence.

    Prose called `broken.png` keeps `image/png`: the text heuristic is a window
    guess and may not overrule a name (review F3, revising D3's first cut, which
    stored `text/plain` here and refused it on the stored type instead). So it
    reaches Pillow, like a truncated PNG that really does carry the signature,
    and both come back as `UnsupportedRendition` — a 400 rather than a 500.
    """
    for name, payload in (
        ("broken.png", b"definitely not a png"),
        ("truncated.png", b"\x89PNG\r\n\x1a\n" + b"cut off here"),
        # The pair review F1 found: once a plugin's `accept()` matches, a failed
        # parse is a **bare** OSError, not `UnidentifiedImageError`.
        ("short.bmp", b"BM" + b"not really a bitmap, no NULs here"),
        ("cut.webp", b"RIFF\x1a\x00\x00\x00WEBPVP8L" + b"\x00" * 8),
    ):
        source = tmp_path / name
        source.write_bytes(payload)
        asset = assets.register_asset(source)
        assert asset.mime.startswith("image/"), (name, asset.mime)
        with pytest.raises(UnsupportedRendition):
            assets.get_rendition(asset.hash, principal=owner())


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        # `UnidentifiedImageError` is an OSError *subclass*, so catching only it
        # let its siblings out as `EXCEPTION_STATUS[OSError]` — a 500 on a route
        # with no session, which also spent the caller's upload token (F1).
        ("short.bmp", b"BM" + b"not really a bitmap, no NULs here"),
        ("cut.webp", b"RIFF\x1a\x00\x00\x00WEBPVP8L" + b"\x00" * 8),
        ("nothing.png", b"\x89PNG\r\n\x1a\n" + b"cut off here"),
    ],
)
def test_the_pixel_budget_refuses_bytes_pillow_cannot_read_without_leaking_the_path(
    tmp_path, name, payload
):
    """One refusal class for every unreadable image, and no spool path in it.

    The path is the operator's on a terminal and a stranger's over a socket
    (review F6): the network-facing message names the filename the client
    supplied, exactly as the sibling rendition refusal does.
    """
    spooled = tmp_path / name
    spooled.write_bytes(payload)

    with pytest.raises(UnsupportedRendition) as refused:
        assets.check_image_pixel_budget(spooled, name=name)

    assert str(refused.value) == f"not a raster image Pillow can read: {name}"
    assert str(tmp_path) not in str(refused.value)
    # On the CLI the path is the useful answer, so it is still the default.
    with pytest.raises(UnsupportedRendition, match=str(spooled)):
        assets.check_image_pixel_budget(spooled)


def test_the_pixel_ceiling_is_optional_and_the_bomb_guard_is_not(tmp_path, monkeypatch):
    """Two questions of one header read (review F9).

    `limit=None` is for a route admitting bytes to *ingest*, where 40 MP is a
    statement about renditions and a 600 dpi A3 scan is an ordinary document.
    What stays is the guard about danger: Pillow's own bomb refusal, which
    `limit=None` does not switch off. Pillow's threshold is lowered here rather
    than met, since meeting it means allocating 179 megapixels.
    """
    scan = tmp_path / "scan.png"
    Image.new("L", (7000, 7000)).save(scan, "PNG")  # 49 MP, over the 40 MP ceiling

    assert assets.check_image_pixel_budget(scan, limit=None) == (7000, 7000)
    with pytest.raises(ImageTooLarge, match="the limit is"):
        assets.check_image_pixel_budget(scan)

    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    with pytest.raises(ImageTooLarge, match="decompression bomb"):
        assets.check_image_pixel_budget(scan, limit=None)


@pytest.mark.parametrize("profile", ["banner", "page", "page:0", "page:01", "page:-1", "PAGE:1"])
def test_unknown_profile_is_rejected(fresh_db, tmp_path, profile):
    """Anything that is not a static profile or a well-formed `page:<n>`.

    `page:0` and `page:01` are the interesting ones: page numbers are 1-based,
    and one spelling per page keeps two names from keying two rendition rows
    onto the same bitmap.
    """
    asset = _register_image(fresh_db, tmp_path)
    with pytest.raises(UnsupportedRendition, match="unknown rendition profile"):
        assets.get_rendition(asset.hash, profile=profile, principal=owner())


def test_rendition_of_missing_asset_raises(fresh_db):
    with pytest.raises(AssetNotFound):
        assets.get_rendition("missing", principal=owner())


# ── `page:<n>` PDF rasters (design §5.7) ──────────────────────────────────────


def test_resolve_profile_separates_static_profiles_from_pages():
    """Pure name resolution — no database, so no fixture."""
    assert assets.resolve_profile("thumb") == (assets.PROFILES["thumb"], None)
    assert assets.resolve_profile("preview") == (assets.PROFILES["preview"], None)
    assert assets.resolve_profile("page:1") == (assets.PAGE_PROFILE, 1)
    assert assets.resolve_profile("page:42") == (assets.PAGE_PROFILE, 42)
    with pytest.raises(UnsupportedRendition, match=r"have: preview, thumb, page:<n>"):
        assets.resolve_profile("page:0")


def test_page_raster_renders_a_letter_page_at_144_dpi(fresh_db, tmp_path):
    """612×792 canvas units × (144/72) = 1224×1584, inside the 1568 cap on the
    long edge only — so the page comes back scaled to exactly that cap."""
    asset = _register_pdf(tmp_path)
    rendition = assets.get_rendition(asset.hash, profile="page:1", principal=owner())

    assert rendition.profile == "page:1"
    assert rendition.height == assets.PAGE_PROFILE.max_edge == 1568
    assert rendition.width == round(1224 * 1568 / 1584)
    assert rendition.size_bytes <= assets.PAGE_PROFILE.target_bytes
    with _decode(rendition) as decoded:
        assert decoded.format == "WEBP"
        assert decoded.size == (rendition.width, rendition.height)


def test_each_page_renders_its_own_bitmap(fresh_db, tmp_path):
    """Different pages must not collapse onto one another's rendition row."""
    asset = _register_pdf(tmp_path, pages=("alpha", "beta", "gamma"))
    stored = {}
    for page in (1, 2, 3):
        rendition = assets.get_rendition(asset.hash, profile=f"page:{page}", principal=owner())
        stored[page] = assets.read_rendition_bytes(rendition)
    assert len({value for value in stored.values()}) == 3


def test_page_raster_is_addressed_cached_and_purged_like_any_rendition(fresh_db, tmp_path):
    asset = _register_pdf(tmp_path)
    generated = assets.get_rendition(asset.hash, profile="page:2", principal=owner())
    assert generated.cached is False
    assert generated.id == hashlib.sha256(f"{asset.hash}:page:2".encode()).hexdigest()
    assert generated.mime == "image/webp"

    hit = assets.get_rendition(asset.hash, profile="page:2", principal=owner())
    assert hit.cached is True
    assert hit.id == generated.id

    conn = db.connect(fresh_db)
    try:
        row = conn.execute(
            "SELECT profile, asset_hash, size_bytes FROM renditions WHERE id = ?",
            (generated.id,),
        ).fetchone()
    finally:
        conn.close()
    assert (row["profile"], row["asset_hash"]) == ("page:2", asset.hash)
    assert row["size_bytes"] == generated.size_bytes

    result = assets.purge_renditions(asset_hash=asset.hash)
    assert result.purged == 1
    assert assets.get_rendition(asset.hash, profile="page:2", principal=owner()).cached is False


def test_page_raster_of_a_non_pdf_is_refused(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path)
    with pytest.raises(UnsupportedRendition, match="only supported for PDF assets"):
        assets.get_rendition(asset.hash, profile="page:1", principal=owner())


def test_static_profile_of_a_pdf_is_refused(fresh_db, tmp_path):
    """The inverse refusal, and it names the profile that would have worked."""
    asset = _register_pdf(tmp_path)
    for profile in ("thumb", "preview"):
        with pytest.raises(UnsupportedRendition, match="page:<n>") as raised:
            assets.get_rendition(asset.hash, profile=profile, principal=owner())
        assert "application/pdf" in str(raised.value)


def test_page_past_the_end_names_the_page_count(fresh_db, tmp_path):
    asset = _register_pdf(tmp_path, pages=("alpha", "beta"))
    with pytest.raises(UnsupportedRendition, match="it has 2 page"):
        assets.get_rendition(asset.hash, profile="page:3", principal=owner())


def test_a_page_that_would_blow_the_pixel_budget_is_refused_before_rendering(fresh_db, tmp_path):
    """PDF allows a 200×200 inch page; at 144 DPI that is 829 megapixels.

    The refusal is arithmetic — page geometry × the DPI scale — because PDFium
    allocates the whole bitmap before it draws anything, so there is no header
    to read and no partial decode to abandon.
    """
    asset = _register_pdf(tmp_path, name="poster.pdf", width=14400, height=14400)
    with pytest.raises(ImageTooLarge, match="28800×28800"):
        assets.get_rendition(asset.hash, profile="page:1", principal=owner())


def test_unreadable_pdf_bytes_are_refused_cleanly(fresh_db, tmp_path):
    fake = tmp_path / "broken.pdf"
    fake.write_bytes(b"%PDF-1.4 and then nothing that parses")
    asset = assets.register_asset(fake)
    with pytest.raises(UnsupportedRendition, match="cannot render this PDF"):
        assets.get_rendition(asset.hash, profile="page:1", principal=owner())


def test_without_pypdfium2_a_page_request_names_the_extra(fresh_db, tmp_path):
    """The branch most installs take: the `pdf` extra is optional.

    `None` in `sys.modules` is what an absent module looks like to an `import`
    statement, so this exercises the real lazy import rather than a seam
    invented for the test. Its own MonkeyPatch context, because undoing the
    shared one would also undo `fresh_db`'s NODUM_DB.
    """
    asset = _register_pdf(tmp_path)
    with pytest.MonkeyPatch.context() as absent:
        absent.setitem(sys.modules, "pypdfium2", None)
        with pytest.raises(UnsupportedRendition, match="install the 'pdf' extra"):
            assets.get_rendition(asset.hash, profile="page:1", principal=owner())

    # Nothing was cached on the way out, so the extra arriving later just works.
    assert assets.get_rendition(asset.hash, profile="page:1", principal=owner()).cached is False


def test_a_pdf_is_as_reachable_as_its_describing_node(fresh_db, tmp_path):
    """Page rasters obey the module's access rule like every other rendition."""
    asset = _register_pdf(tmp_path)
    reader = agent("pdf-reader", grants={"main": "read"})
    with pytest.raises(AssetNotFound):
        assets.get_rendition(asset.hash, profile="page:1", principal=reader)

    _describe(asset)
    assert (
        assets.get_rendition(asset.hash, profile="page:1", principal=reader).asset_hash
        == asset.hash
    )


def test_page_rasters_leave_image_profiles_alone(fresh_db, tmp_path):
    """`thumb`/`preview` on an image are untouched by the new resolution path."""
    asset = _register_image(fresh_db, tmp_path, size=(2000, 1000))
    thumb = assets.get_rendition(asset.hash, profile="thumb", principal=owner())
    preview = assets.get_rendition(asset.hash, profile="preview", principal=owner())
    assert (thumb.width, thumb.height) == (256, 128)
    assert (preview.width, preview.height) == (1024, 512)
    assert thumb.id == hashlib.sha256(f"{asset.hash}:thumb".encode()).hexdigest()


# ── Extracted text (the ingestion pipeline's write) ───────────────────────────


def test_set_extracted_text_round_trips(fresh_db, tmp_path):
    asset = _register_pdf(tmp_path)
    assert assets.get_asset(asset.hash, principal=owner()).extracted_text is None

    assets.set_extracted_text(asset.hash, "the whole text of the paper")
    assert (
        assets.get_asset(asset.hash, principal=owner()).extracted_text
        == "the whole text of the paper"
    )

    # Clearing is distinct from never having run an extraction only in intent;
    # both read back as None, and neither is an error.
    assets.set_extracted_text(asset.hash, None)
    assert assets.get_asset(asset.hash, principal=owner()).extracted_text is None


def test_set_extracted_text_on_an_unknown_hash_raises(fresh_db):
    with pytest.raises(AssetNotFound, match="asset not found"):
        assets.set_extracted_text("missing", "text")


# ── Streaming and the single-file promise ─────────────────────────────────────


def test_open_original_streams_the_stored_bytes(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path)
    original = (tmp_path / "photo.png").read_bytes()
    conn = db.connect(fresh_db)
    try:
        with assets.open_original(conn, asset.hash) as blob:
            assert len(blob) == len(original)
            blob.seek(0)
            head = blob.read(16)
            blob.seek(0)
            assert blob.read() == original
        assert head == original[:16]
    finally:
        conn.close()


def test_open_original_of_missing_asset_raises(fresh_db):
    conn = db.connect(fresh_db)
    try:
        with pytest.raises(AssetNotFound):
            assets.open_original(conn, "missing")
    finally:
        conn.close()


def test_vacuum_into_snapshot_carries_originals_and_renditions(fresh_db, tmp_path, monkeypatch):
    """DB = everything: a one-file backup restores the binaries with the graph."""
    asset = _register_image(fresh_db, tmp_path)
    rendition = assets.get_rendition(asset.hash, profile="thumb", principal=owner())
    original = (tmp_path / "photo.png").read_bytes()

    snapshot = tmp_path / "backup" / "graph.db"
    snapshot.parent.mkdir()
    conn = db.connect(fresh_db)
    try:
        conn.execute("VACUUM INTO ?", (str(snapshot),))
    finally:
        conn.close()

    # Nothing but the one file is carried over.
    monkeypatch.setenv("NODUM_DB", str(snapshot))
    assert assets.get_asset(asset.hash, principal=owner()).hash == asset.hash
    restored = db.connect(snapshot)
    try:
        with assets.open_original(restored, asset.hash) as blob:
            assert blob.read() == original
    finally:
        restored.close()
    assert assets.read_rendition_bytes(rendition)[:4] == b"RIFF"


def test_register_refuses_a_source_that_shrank_between_passes(fresh_db, tmp_path, monkeypatch):
    """Registration reads the file twice; the stored bytes must match the key.

    A file still being written (a rotating log, a partial download) can be
    shorter on the copy pass than on the hash pass. The blob keeps its
    zero-filled tail, so the row would commit with
    `sha256(stored) != assets.hash` — silently, and forever.
    """
    source = tmp_path / "rotating.log"
    source.write_bytes(b"the whole payload, every byte of it")
    hash_file = assets._hash_file

    def hash_then_truncate(path):
        digest_and_size = hash_file(path)
        path.write_bytes(b"truncated")  # the writer rotates the file underneath us
        return digest_and_size

    # Scoped, so restoring `_hash_file` cannot also undo `fresh_db`'s NODUM_DB.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(assets, "_hash_file", hash_then_truncate)
        with pytest.raises(
            assets.AssetSourceChanged, match="changed while it was being registered"
        ):
            assets.register_asset(source)

    assert assets.list_assets(principal=owner()) == []
    conn = db.connect(fresh_db)
    try:
        assert conn.execute("SELECT count(*) AS n FROM asset_blobs").fetchone()["n"] == 0
    finally:
        conn.close()


def test_register_refuses_a_source_that_grew_between_passes(fresh_db, tmp_path, monkeypatch):
    """A source that GROWS between the two passes is refused like a shrink/rewrite.

    The extra bytes would overrun the pre-sized blob and surface as a raw
    `ValueError: data longer than blob length`; registration must raise the
    tidy `AssetSourceChanged` the other cases raise, with nothing committed.
    """
    source = tmp_path / "growing.log"
    source.write_bytes(b"small")
    hash_file = assets._hash_file

    def hash_then_grow(path):
        digest_and_size = hash_file(path)
        path.write_bytes(b"small plus a great deal of freshly appended data")  # writer keeps going
        return digest_and_size

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(assets, "_hash_file", hash_then_grow)
        with pytest.raises(
            assets.AssetSourceChanged, match="changed while it was being registered"
        ):
            assets.register_asset(source)

    assert assets.list_assets(principal=owner()) == []
    conn = db.connect(fresh_db)
    try:
        assert conn.execute("SELECT count(*) AS n FROM asset_blobs").fetchone()["n"] == 0
    finally:
        conn.close()


def test_stored_bytes_always_hash_to_their_key(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path)
    conn = db.connect(fresh_db)
    try:
        with assets.open_original(conn, asset.hash) as blob:
            stored = blob.read()
    finally:
        conn.close()
    assert hashlib.sha256(stored).hexdigest() == asset.hash
    assert len(stored) == asset.size_bytes


def test_oversized_asset_is_refused_with_a_clear_message(fresh_db, tmp_path, monkeypatch):
    """The blob-length ceiling is named up front, not as `blob too big`."""
    source = tmp_path / "big.bin"
    source.write_bytes(b"x" * 64)
    monkeypatch.setattr(assets, "max_blob_bytes", lambda conn: 16)

    with pytest.raises(assets.AssetTooLarge, match="cannot exceed SQLite's 16-byte blob limit"):
        assets.register_asset(source)
    assert assets.list_assets(principal=owner()) == []


def test_register_missing_file_raises_file_not_found(fresh_db, tmp_path):
    with pytest.raises(FileNotFoundError):
        assets.register_asset(tmp_path / "nope.png")


def test_registration_rolls_back_when_the_blob_write_fails(fresh_db, tmp_path, monkeypatch):
    """A crash mid-copy must leave no asset row behind."""
    source = _make_image(tmp_path / "photo.png")

    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("disk gone")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(assets, "open_original", explode)
        with pytest.raises(sqlite3.OperationalError):
            assets.register_asset(source)

    assert assets.list_assets(principal=owner()) == []
    conn = db.connect(fresh_db)
    try:
        assert conn.execute("SELECT count(*) AS n FROM asset_blobs").fetchone()["n"] == 0
    finally:
        conn.close()
