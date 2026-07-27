"""The ingestion pipeline: bytes in, reviewable subgraph out (design §5.5–§5.7).

The golden case is note 01's exit criterion in miniature — a real PDF ingested
end to end and asserted down to the ``derived_from`` chain and the FTS row.
Everything else here is the failure and recovery behaviour that criterion
depends on: idempotency, partial-run repair, scoping, and a fetch path that
never touches the network.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from helpers import agent, owner, seed_space

from nodum import assets, db, extract, ingest, projectors, search, service

#: The committed two-page PDF. Its words appear nowhere else in the suite, so a
#: search hit for one of them can only have come through extraction.
FIXTURE_PDF = Path(__file__).parent / "fixtures" / "sample.pdf"

#: A word that exists only on page 1 of that PDF.
PDF_WORD = "Ostrogothic"


@pytest.fixture(autouse=True)
def _fresh_extraction_probes():
    """Handler availability is cached process-wide; do not leak it between tests."""
    extract.reset_availability()
    yield
    extract.reset_availability()


def _nodes(type_id: str, principal, **kwargs):
    return service.list_nodes(type=type_id, principal=principal, **kwargs)


# ── The golden ingestion ─────────────────────────────────────────────────────


@pytest.mark.skipif(
    extract.handler_for("application/pdf") is None or not extract.PdfHandler().availability()[0],
    reason="the pdf extra is not installed",
)
def test_a_pdf_becomes_a_reviewable_subgraph(fresh_db):
    """Note 01's exit criterion for one file: asset, describing node, source,
    provenance edge, page structure, and the text findable by search."""
    result = ingest.ingest_file(FIXTURE_PDF, principal=owner())

    assert result.created is True
    assert result.extraction.handler == "pdf"
    assert result.extraction.pages == 2
    assert PDF_WORD in result.source.content

    # The bytes are registered, deduped by sha256, and carry the extracted text.
    stored = assets.get_asset(result.asset.hash, principal=owner())
    assert stored.mime == "application/pdf"
    assert PDF_WORD in stored.extracted_text

    # The describing node is what makes the asset reachable at all.
    assert result.asset_ref.type == "asset_ref"
    assert result.asset_ref.props["asset_hash"] == result.asset.hash
    assert result.asset_ref.props["extracted_by"] == "pdf"

    # source --derived_from--> asset_ref, and nothing else.
    assert len(result.edges) == 1
    edge = result.edges[0]
    assert (edge.src_id, edge.dst_id, edge.type) == (
        result.source.id,
        result.asset_ref.id,
        "derived_from",
    )

    # One block per page of text, in order, numbered.
    assert [page.title for page in result.pages] == ["Page 1", "Page 2"]
    assert [page.props["page"] for page in result.pages] == [1, 2]
    assert all(page.parent_id == result.source.id for page in result.pages)
    assert result.pages_truncated is False


@pytest.mark.skipif(
    not extract.PdfHandler().availability()[0], reason="the pdf extra is not installed"
)
def test_extracted_text_reaches_search(fresh_db):
    """The whole point of `assets.extracted_text`: a word that appears only
    inside the binary becomes findable once the projector has run."""
    ingest.ingest_file(FIXTURE_PDF, principal=owner())
    projectors.run_projectors(names=["fts"])

    hits = search.search(PDF_WORD, principal=owner())

    assert hits.hits, f"{PDF_WORD!r} should be findable after ingestion"


@pytest.mark.skipif(
    not extract.PdfHandler().availability()[0], reason="the pdf extra is not installed"
)
def test_a_word_on_one_page_does_not_match_every_other_page(fresh_db):
    """The extracted-text join belongs to the `asset_ref` node alone.

    Ingestion records `asset_hash` on the source and on every page block too —
    real provenance, and what lets a page id resolve to a `page:<n>` raster —
    so a join keyed on the prop alone gave every page of a document the whole
    document's text. A word from page 1 then matched pages 1 and 2 equally and
    per-page precision was gone.
    """
    result = ingest.ingest_file(FIXTURE_PDF, principal=owner())
    projectors.run_projectors(names=["fts"])
    page_one, page_two = result.pages

    hits = {hit.node_id for hit in search.search(PDF_WORD, principal=owner()).hits}

    assert page_one.id in hits, "the page carrying the word must match"
    assert page_two.id not in hits, "a page that does not carry the word must not"
    # The document-level nodes still match: the asset_ref through the join,
    # the source through its own content.
    assert {result.asset_ref.id, result.source.id} <= hits


# ── Idempotency and repair ───────────────────────────────────────────────────


def test_ingesting_the_same_file_twice_changes_nothing(fresh_db, tmp_path):
    """0009 allows one live `asset_ref` per (hash, space): a re-run has to find
    the describing node, not trip the index."""
    source = tmp_path / "note.txt"
    source.write_text("Vercingetorix basin hydrology", encoding="utf-8")

    first = ingest.ingest_file(source, principal=owner())
    second = ingest.ingest_file(source, principal=owner())

    assert first.created is True
    assert second.created is False
    assert second.asset_ref.id == first.asset_ref.id
    assert second.source.id == first.source.id
    assert second.event_seq == 0, "a no-op ingestion writes no event"
    assert len(_nodes("asset_ref", owner())) == 1
    assert len(_nodes("source", owner())) == 1


def test_a_run_interrupted_between_the_two_nodes_is_repaired_by_rerunning(fresh_db, tmp_path):
    """The reason the gate checks both nodes rather than just the describing
    one: a half-finished ingestion has to converge, not deadlock on the index."""
    source = tmp_path / "note.txt"
    source.write_text("quicksilver cartography", encoding="utf-8")
    first = ingest.ingest_file(source, principal=owner())
    service.transition(first.source.id, "archive", principal=owner())

    repaired = ingest.ingest_file(source, principal=owner())

    assert repaired.asset_ref.id == first.asset_ref.id, "the describing node is reused"
    assert repaired.source.id != first.source.id, "the retired source is replaced"
    assert len(_nodes("asset_ref", owner(), state="active")) == 1


def test_an_archived_describing_node_frees_its_hash(fresh_db, tmp_path):
    """0009's index skips archived rows on purpose, so retiring an `asset_ref`
    must let a fresh ingestion of the same bytes describe them again."""
    source = tmp_path / "note.txt"
    source.write_text("marginalia", encoding="utf-8")
    first = ingest.ingest_file(source, principal=owner())
    service.transition(first.asset_ref.id, "archive", principal=owner())
    service.transition(first.source.id, "archive", principal=owner())

    again = ingest.ingest_file(source, principal=owner())

    assert again.created is True
    assert again.asset_ref.id != first.asset_ref.id


# ── Degradation ──────────────────────────────────────────────────────────────


def test_a_file_no_handler_claims_is_still_registered_and_described(fresh_db, tmp_path):
    """Note 01 D2: `nodum ingest` on a machine without a handler must still
    register the asset, describe it, and say plainly that no text came out."""
    source = tmp_path / "mystery.bin"
    source.write_bytes(b"\x00\x01\x02\x03")

    result = ingest.ingest_file(source, principal=owner())

    assert result.created is True
    assert result.extraction.handler == "none"
    assert result.extraction.chars == 0
    assert "no extraction handler" in result.extraction.detail
    assert result.asset_ref.props["asset_hash"] == result.asset.hash
    assert result.source.content == ""
    assert assets.get_asset(result.asset.hash, principal=owner()).extracted_text is None


def test_a_handler_whose_dependency_is_absent_does_not_stop_ingestion(
    fresh_db, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        extract.PdfHandler, "availability", lambda self: (False, "pypdf is not installed")
    )
    extract.reset_availability()
    source = tmp_path / "paper.pdf"
    source.write_bytes(FIXTURE_PDF.read_bytes())

    result = ingest.ingest_file(source, principal=owner())

    assert result.created is True
    assert result.extraction.chars == 0
    assert result.extraction.detail == "pypdf is not installed"
    assert result.pages == []


# ── Page structure ───────────────────────────────────────────────────────────


def test_blank_pages_do_not_become_empty_nodes(fresh_db, tmp_path, monkeypatch):
    """A scanned PDF with no OCR available would otherwise propose a hundred
    empty nodes; the page number stays honest instead of being renumbered."""
    monkeypatch.setattr(
        extract,
        "extract",
        lambda source, *, mime: extract.Extraction(
            handler="pdf", text="one\n\nthree", pages=["one", "   ", "three"]
        ),
    )
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF\n")

    result = ingest.ingest_file(source, principal=owner())

    assert [page.props["page"] for page in result.pages] == [1, 3]


def test_page_blocks_stop_at_the_cap_and_say_so(fresh_db, tmp_path, monkeypatch):
    """Under a `suggest` grant every page block is a review-queue row, so the
    cap is real — and never silent."""
    monkeypatch.setattr(ingest, "MAX_PAGE_BLOCKS", 3)
    monkeypatch.setattr(
        extract,
        "extract",
        lambda source, *, mime: extract.Extraction(
            handler="pdf", text="x", pages=[f"page {n}" for n in range(1, 11)]
        ),
    )
    source = tmp_path / "long.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF\n")

    result = ingest.ingest_file(source, principal=owner())

    assert len(result.pages) == 3
    assert result.pages_truncated is True


def test_the_already_ingested_branch_reports_page_blocks_and_nothing_else(
    fresh_db, tmp_path, monkeypatch
):
    """Review F2: `pages` was every child of the source, of any type and any state.

    A `source` is an ordinary node, so anything can be written under it — a note
    a human filed there, a page block since retired — and `list_children`
    filters on neither type nor state. Every consumer reads this one field, so
    the answer was wrong on the CLI, over MCP and in the browser at once.
    """
    monkeypatch.setattr(
        extract,
        "extract",
        lambda source, *, mime: extract.Extraction(
            handler="pdf", text="one two", pages=["one", "two"]
        ),
    )
    source_file = tmp_path / "scan.pdf"
    source_file.write_bytes(b"%PDF-1.4\n%%EOF\n")
    first = ingest.ingest_file(source_file, principal=owner())
    assert [page.props["page"] for page in first.pages] == [1, 2]

    # An annotation filed under the document, and one page retired.
    service.create_node(
        type="note",
        title="Marginalia",
        content="my own reading",
        parent_id=first.source.id,
        principal=owner(),
    )
    service.transition(first.pages[1].id, "archive", principal=owner())

    # The second drop takes the already-ingested branch, which re-extracts
    # nothing — so what it reports about pages is a read of the graph alone.
    again = ingest.ingest_file(source_file, principal=owner())

    assert again.created is False
    assert [page.id for page in again.pages] == [first.pages[0].id]
    assert {page.type for page in again.pages} == {ingest.PAGE_TYPE}
    assert again.pages_truncated is False


def test_the_already_ingested_branch_still_reports_a_truncated_document(
    fresh_db, tmp_path, monkeypatch
):
    """The cap's effect has to survive the second drop (review F2).

    `pages_truncated` was hard-coded false on this branch, so a 900-page scan
    reported the cap on the first ingestion and denied it on the next — a silent
    truncation, which is exactly what `MAX_PAGE_BLOCKS` promises never to be.
    """
    monkeypatch.setattr(ingest, "MAX_PAGE_BLOCKS", 3)
    monkeypatch.setattr(
        extract,
        "extract",
        lambda source, *, mime: extract.Extraction(
            handler="pdf", text="x", pages=[f"page {n}" for n in range(1, 11)]
        ),
    )
    source_file = tmp_path / "long.pdf"
    source_file.write_bytes(b"%PDF-1.4\n%%EOF\n")

    first = ingest.ingest_file(source_file, principal=owner())
    again = ingest.ingest_file(source_file, principal=owner())

    assert (first.pages_truncated, again.pages_truncated) == (True, True)
    assert len(again.pages) == 3


def test_a_doomed_ingestion_stores_no_bytes(fresh_db, tmp_path):
    """Review F13: nothing irreversible happens before a refusal that needs no bytes.

    Registration is the irreversible half — there is no delete route — and it ran
    before the space was resolved, so a target that stopped resolving left bytes
    with no describing node and no way to reclaim them.
    """
    source = tmp_path / "note.txt"
    source.write_text("filed nowhere", encoding="utf-8")

    with pytest.raises(service.TypeNotFound):
        ingest.ingest_file(source, space="never-created", principal=owner())

    assert assets.list_assets(principal=owner()) == []
    conn = db.connect(fresh_db)
    try:
        assert conn.execute("SELECT count(*) AS n FROM asset_blobs").fetchone()["n"] == 0
    finally:
        conn.close()


def test_a_long_body_is_capped_with_a_visible_marker(fresh_db, tmp_path, monkeypatch):
    """The node content is the embedding input; the full text is never lost —
    it stays on the asset for BM25."""
    monkeypatch.setattr(ingest, "SOURCE_CONTENT_CHARS", 20)
    source = tmp_path / "long.txt"
    source.write_text("y" * 500, encoding="utf-8")

    result = ingest.ingest_file(source, principal=owner())

    assert result.source.content.startswith("y" * 20)
    assert "truncated" in result.source.content
    assert len(assets.get_asset(result.asset.hash, principal=owner()).extracted_text) == 500


# ── Grants and scope ─────────────────────────────────────────────────────────


def test_an_agent_with_suggest_ingests_into_the_review_queue(fresh_db, tmp_path):
    """Ingestion adds no authority of its own: the landing state is the
    principal's grant, exactly as for a hand-written node."""
    source = tmp_path / "note.txt"
    source.write_text("proposal", encoding="utf-8")

    result = ingest.ingest_file(source, principal=agent("researcher"))

    assert result.asset_ref.state == "proposed"
    assert result.source.state == "proposed"
    assert result.edges[0].state == "proposed"


def test_an_agent_without_a_write_grant_on_the_space_is_refused(fresh_db, tmp_path):
    source = tmp_path / "note.txt"
    source.write_text("secret", encoding="utf-8")
    # `meta: read` is needed to resolve the type vocabulary at all; the point
    # of the fixture is the missing *write* grant on the content space.
    reader = agent("reader", grants={"meta": "read", "main": "read"})

    with pytest.raises(service.GrantNotPermitted):
        ingest.ingest_file(source, principal=reader)


def test_an_asset_is_invisible_to_an_agent_that_cannot_reach_its_describing_node(
    fresh_db, tmp_path
):
    """The unreachable case note 01 asked to be in the fixtures from day one."""
    seed_space("research")
    source = tmp_path / "note.txt"
    source.write_text("scoped", encoding="utf-8")
    ingest.ingest_file(
        source,
        space="research",
        principal=agent("writer", grants={"meta": "read", "research": "edit"}),
    )
    outsider = agent("outsider", grants={"meta": "read", "main": "read"})

    assert assets.list_assets(principal=outsider) == []
    with pytest.raises(assets.AssetNotFound):
        assets.get_asset(assets.list_assets(principal=owner())[0].hash, principal=outsider)


# ── The event ────────────────────────────────────────────────────────────────


def test_one_ingestion_writes_one_event_and_no_bytes(fresh_db, tmp_path):
    source = tmp_path / "note.txt"
    source.write_text("hydrology", encoding="utf-8")

    result = ingest.ingest_file(source, principal=owner())

    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM events WHERE op = 'asset.ingest'").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["asset_hash"] == result.asset.hash
    assert payload["source_id"] == result.source.id
    assert payload["handler"] == "text"
    # The rule the asset store has carried since 0007.
    assert "data" not in payload and "bytes" not in payload
    assert len(rows[0]["payload"]) < 4096


def test_an_ingest_event_cannot_be_undone(fresh_db, tmp_path):
    """`asset.*` events are audit records, not reversible graph mutations."""
    source = tmp_path / "note.txt"
    source.write_text("audit", encoding="utf-8")
    result = ingest.ingest_file(source, principal=owner())

    with pytest.raises(ValueError):
        service.undo(result.event_seq, principal=owner())


# ── ingest_url, against a loopback fixture server only ───────────────────────


class _Handler(BaseHTTPRequestHandler):
    """Serves one canned response; no logging into the test output."""

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's own naming
        body, content_type = self.server.canned
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


@pytest.fixture()
def fixture_server():
    """A loopback HTTP server serving one canned body. The suite never leaves the machine."""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.canned = (b"", "text/plain")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _url(server, path: str) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}{path}"


def test_ingest_url_extracts_html_and_records_its_provenance(fresh_db, fixture_server):
    fixture_server.canned = (
        b"<html><head><style>b{}</style></head><body><p>Basin hydrology</p></body></html>",
        "text/html; charset=utf-8",
    )

    result = ingest.ingest_url(_url(fixture_server, "/article"), principal=owner())

    assert result.extraction.handler == "html"
    assert result.source.content == "Basin hydrology"
    assert result.asset_ref.props["url"].endswith("/article")
    assert result.source.props["url"].endswith("/article")


def test_an_extensionless_url_still_reaches_a_handler(fresh_db, fixture_server):
    """`register_asset` derives the MIME from the name, so a document served
    from an extensionless path would otherwise be stored as octet-stream and
    reach no handler at all."""
    fixture_server.canned = (b"<p>text</p>", "text/html")

    result = ingest.ingest_url(_url(fixture_server, "/article"), principal=owner())

    assert result.asset.mime == "text/html"
    assert result.extraction.handler == "html"


def test_a_url_with_its_own_filename_keeps_it(fresh_db, fixture_server):
    fixture_server.canned = (b"plain body", "text/plain")

    result = ingest.ingest_url(_url(fixture_server, "/papers/basin.txt"), principal=owner())

    assert result.asset.original_name == "basin.txt"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.invalid/x", "/local/path"])
def test_ingest_url_only_fetches_http_and_https(fresh_db, url):
    with pytest.raises(ingest.IngestError, match="ingest_url takes"):
        ingest.ingest_url(url, principal=owner())


def test_a_body_over_the_ceiling_is_refused_mid_stream(fresh_db, fixture_server, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_FETCH_BYTES", 16)
    fixture_server.canned = (b"z" * 1024, "text/plain")

    with pytest.raises(ingest.IngestError, match="fetch ceiling"):
        ingest.ingest_url(_url(fixture_server, "/big.txt"), principal=owner())


def test_an_unreachable_host_is_an_ingest_error_not_a_traceback(fresh_db):
    with pytest.raises(ingest.IngestError, match="could not fetch"):
        ingest.ingest_url("http://127.0.0.1:1/nothing", principal=owner())


def test_a_local_path_that_is_not_a_file_is_refused(fresh_db, tmp_path):
    with pytest.raises(ingest.IngestError, match="not a file"):
        ingest.ingest_file(tmp_path, principal=owner())
