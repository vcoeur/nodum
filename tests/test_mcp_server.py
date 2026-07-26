"""MCP server tests: every registered tool round-trips through the MCP layer.

The server is exercised in-process over memory streams
(``create_connected_server_and_client_session``) — the same handlers stdio
clients reach, no subprocess needed.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from helpers import agent, owner, seed_space
from mcp.shared.memory import create_connected_server_and_client_session
from PIL import Image

from nodum import assets, auth, ingest, mcp_server, service, urls
from nodum.mcp_server import (
    ADDITIVE_TOOLS,
    CURATIVE_TOOLS,
    OVERWRITING_TOOLS,
    READ_TOOLS,
    REVIEW_TOOLS,
    create_server,
)

AGENT = "agent:tester"
TOKEN = "ndm_test_token_for_the_mcp_suite"

#: The Phase-4 ingestion tools, named literally: the tier constants are the
#: registry's contract, and a test that only walked them would pass just as
#: happily with a tool missing from both.
INGEST_TOOLS = ("ingest_file", "ingest_url", "request_upload_url")

#: The committed two-page PDF, shared with the ingestion suite.
FIXTURE_PDF = Path(__file__).parent / "fixtures" / "sample.pdf"


def _run(fn, *, actor: str = AGENT, token: str = TOKEN, grants: dict[str, str] | None = None):
    """Run an async MCP interaction against a fresh server bound to one agent.

    Defaults to AGENT with the helper's parity grants (``meta`` read, ``main``
    suggest); ``actor``/``token``/``grants`` mint a differently scoped agent for
    the tests about what a grant does not reach.
    """

    async def runner():
        agent(actor, token=token, grants=grants)  # seed the account with a verifiable token
        server = create_server(token=token)
        async with create_connected_server_and_client_session(server) as session:
            return await fn(session)

    return asyncio.run(runner())


def _call(session, tool, arguments=None):
    return session.call_tool(tool, arguments or {})


# ── Tool registry: tiers and annotations ──────────────────────────────────────


def test_registered_tools_are_exactly_the_read_and_additive_tiers(fresh_db):
    tools = _run(lambda session: session.list_tools()).tools
    names = {tool.name for tool in tools}
    assert names == set(READ_TOOLS) | set(ADDITIVE_TOOLS)
    # §8.2 structural enforcement: curative tools can never appear over MCP.
    assert names.isdisjoint(CURATIVE_TOOLS)


def test_review_tools_are_never_registered(fresh_db):
    """The §8.1 human tier is unreachable: an agent cannot review at all."""
    tools = _run(lambda session: session.list_tools()).tools
    names = {tool.name for tool in tools}
    assert names.isdisjoint(REVIEW_TOOLS)

    async def scenario(session):
        return [
            await _call(session, "accept", {"ids": ["whatever"]}),
            await _call(session, "reject", {"ids": ["whatever"], "reason": "mine now"}),
        ]

    for result in _run(scenario):
        assert result.isError
        assert "unknown tool" in result.content[0].text.lower()


def test_tool_annotations(fresh_db):
    tools = _run(lambda session: session.list_tools()).tools
    by_name = {tool.name: tool for tool in tools}
    for name in READ_TOOLS:
        assert by_name[name].annotations.readOnlyHint is True
        assert by_name[name].annotations.destructiveHint is False
    # Additive tools only ever add state, whatever grant the agent holds, so
    # `destructiveHint=False` is honest under `edit` as well as `suggest`.
    # `update_node` is not: with `edit` it overwrites the node in place, and
    # hosts auto-approve on that flag (review S15).
    for name in ADDITIVE_TOOLS:
        assert by_name[name].annotations.readOnlyHint is False
        assert by_name[name].annotations.destructiveHint is (name in OVERWRITING_TOOLS)
    assert set(OVERWRITING_TOOLS) <= set(ADDITIVE_TOOLS)


def test_write_tool_docstrings_state_what_an_edit_grant_changes(fresh_db):
    """The tier is additive by *policy*, not by grant: the docs must say so.

    A `suggest` agent's writes queue; an `edit` agent's land live. Tool
    descriptions are what an agent reads before calling, and they used to
    promise `proposed` unconditionally (review S15).
    """
    tools = _run(lambda session: session.list_tools()).tools
    for tool in tools:
        if tool.name in ADDITIVE_TOOLS:
            assert "edit" in tool.description, tool.name


def test_the_ingestion_tools_are_registered_in_the_tiers_the_design_gives_them(fresh_db):
    """§8.1: ingestion is additive; the download URL sits in the *read* tier."""
    names = {tool.name for tool in _run(lambda session: session.list_tools()).tools}

    assert set(INGEST_TOOLS) <= set(ADDITIVE_TOOLS)
    assert "get_download_url" in READ_TOOLS
    assert set(INGEST_TOOLS) | {"get_download_url"} <= names


def test_ingestion_is_annotated_additive_because_it_only_ever_adds(fresh_db):
    """The annotation states the worst case, and ingestion's worst case is *more* state.

    Every graph write in the pipeline is a `create_node`/`create_edge`, so an
    `edit` grant makes the subgraph land `active` instead of `proposed` — it
    never overwrites a live node the way `update_node` does. Hosts auto-approve
    on `destructiveHint=False`, so this has to be true rather than convenient.
    """
    by_name = {tool.name: tool for tool in _run(lambda session: session.list_tools()).tools}

    for name in INGEST_TOOLS:
        assert by_name[name].annotations.readOnlyHint is False, name
        assert by_name[name].annotations.destructiveHint is False, name
        assert name not in OVERWRITING_TOOLS, name

    # Minting a download URL writes a capability row and an audit entry, but no
    # node, edge, or version — nothing another reader can see changes.
    assert by_name["get_download_url"].annotations.readOnlyHint is True
    assert by_name["get_download_url"].annotations.destructiveHint is False


def test_the_grown_registry_still_holds_no_review_or_curative_tool(fresh_db):
    """Four tools bigger, and the two absent tiers are still absent."""
    names = {tool.name for tool in _run(lambda session: session.list_tools()).tools}

    assert names.isdisjoint(REVIEW_TOOLS)
    assert names.isdisjoint(CURATIVE_TOOLS)
    assert names == set(READ_TOOLS) | set(ADDITIVE_TOOLS)


# ── Token authentication: the MCP surface verifies before it serves ───────────


def test_create_server_rejects_an_unknown_token(fresh_db):
    with pytest.raises(auth.InvalidCredentials):
        create_server(token="ndm_not_a_real_token")


def test_create_server_rejects_a_disabled_agents_token(fresh_db):
    created = service.create_agent("bot", owner_human_id="owner", principal=owner())
    service.disable_agent("bot", principal=owner())
    with pytest.raises(auth.InvalidCredentials):
        create_server(token=created.token)


def test_create_server_rejects_a_token_whose_owner_is_disabled(fresh_db):
    created = service.create_agent("bot", owner_human_id="owner", principal=owner())
    service.create_human("second", principal=owner())  # the owner is not the last one
    service.disable_human("owner", principal=owner())
    with pytest.raises(auth.InvalidCredentials):
        create_server(token=created.token)


# ── Additive tier: writes are attributed and land proposed ────────────────────


def test_create_node_lands_proposed_with_the_configured_actor(fresh_db):
    async def scenario(session):
        created = await _call(session, "create_node", {"type": "note", "title": "MCP note"})
        assert not created.isError
        return created.structuredContent

    node = _run(scenario)
    assert node["state"] == "proposed"
    assert node["created_by"] == AGENT


def test_update_node_stages_a_proposed_version(fresh_db):
    note = service.create_node(
        type="note",
        title="Original",
        content="original body",
        principal=owner(),
    )

    async def scenario(session):
        result = await _call(session, "update_node", {"id": note.id, "content": "bot rewrite"})
        assert not result.isError
        return result.structuredContent

    version = _run(scenario)
    assert version["state"] == "proposed"
    assert version["content"] == "bot rewrite"
    assert service.get_node(note.id, principal=owner()).content == "original body"


def test_link_lands_proposed(fresh_db):
    a = service.create_node(type="concept", title="A", principal=owner())
    b = service.create_node(type="concept", title="B", principal=owner())

    async def scenario(session):
        edge = await _call(session, "link", {"src": a.id, "dst": b.id, "edge_type": "mentions"})
        return edge.structuredContent

    edge = _run(scenario)
    assert edge["state"] == "proposed"
    assert edge["created_by"] == AGENT


def test_propose_edges_batch(fresh_db):
    a = service.create_node(type="concept", title="A", principal=owner())
    b = service.create_node(type="concept", title="B", principal=owner())

    async def scenario(session):
        result = await _call(
            session,
            "propose_edges",
            {
                "suggestions": [
                    {"src": a.id, "dst": b.id, "edge_type": "relates_to"},
                    {"src": a.id, "dst": "missing", "edge_type": "supports"},
                ]
            },
        )
        return result.structuredContent

    outcome = _run(scenario)
    assert len(outcome["created"]) == 1
    assert outcome["created"][0]["state"] == "proposed"
    assert outcome["failed"][0]["index"] == 1


# ── Read tier round trips ─────────────────────────────────────────────────────


def _seed():
    a = service.create_node(
        type="concept",
        title="Alpha concept",
        content="graph theory",
        principal=owner(),
    )
    b = service.create_node(
        type="note",
        title="Beta note",
        content="about graph theory",
        principal=owner(),
    )
    child = service.create_node(
        type="block",
        title="Child block",
        parent_id=b.id,
        principal=owner(),
    )
    service.create_edge(a.id, b.id, "supports", confidence=0.9, principal=owner())
    return a, b, child


def test_get_node_with_neighborhood(fresh_db):
    a, b, _ = _seed()
    result = _run(lambda session: _call(session, "get_node", {"id": a.id, "depth": 1}))
    subgraph = result.structuredContent
    assert subgraph["root"] == a.id
    assert {node["id"] for node in subgraph["nodes"]} == {a.id, b.id}
    assert len(subgraph["edges"]) == 1


def test_get_children(fresh_db):
    _, b, child = _seed()
    result = _run(lambda session: _call(session, "get_children", {"id": b.id}))
    assert [node["id"] for node in result.structuredContent["result"]] == [child.id]


def test_search_with_filters_and_expand(fresh_db):
    a, b, _ = _seed()
    result = _run(
        lambda session: _call(
            session, "search", {"query": "graph theory", "filters": {"type": "note"}}
        )
    )
    assert [hit["node_id"] for hit in result.structuredContent["hits"]] == [b.id]

    expanded = _run(
        lambda session: _call(session, "search", {"query": "Alpha concept", "expand": True})
    )
    hits = expanded.structuredContent["hits"]
    assert [hit["node_id"] for hit in hits] == [a.id, b.id]
    assert "graph" in hits[1]["signals"]


def test_search_date_filters_reach_the_query(fresh_db):
    """`created_after`/`created_before` are honoured, not just accepted."""
    _, b, _ = _seed()
    stamp = service.get_node(b.id, principal=owner()).created_at

    def hits(filters):
        arguments = {"query": "graph", "filters": filters}
        result = _run(lambda session: _call(session, "search", arguments))
        return [hit["node_id"] for hit in result.structuredContent["hits"]]

    assert b.id in hits({"created_after": "2000-01-01 00:00:00"})
    assert hits({"created_before": "2000-01-01 00:00:00"}) == []
    assert b.id not in hits({"created_after": stamp})  # bounds are exclusive


def test_search_rejects_an_unknown_filter_key(fresh_db):
    """A typo'd filter is an error, never a silently unfiltered search."""
    _seed()
    result = _run(
        lambda session: _call(session, "search", {"query": "graph", "filters": {"crated_by": "x"}})
    )
    assert result.isError
    assert "unknown search filter" in result.content[0].text


def test_search_includes_the_vector_signal_when_a_provider_exists(fresh_db, fake_embedder):
    _, b, _ = _seed()
    # "Beta note" matches b's title (BM25) and b's chunk vocabulary (vector).
    result = _run(lambda session: _call(session, "search", {"query": "Beta note"}))
    hits = result.structuredContent["hits"]
    assert [hit["node_id"] for hit in hits][:1] == [b.id]
    assert "bm25" in hits[0]["signals"]
    assert "vector" in hits[0]["signals"]
    assert hits[0]["score"] == sum(hits[0]["signals"].values())


def test_traverse_and_find_path(fresh_db):
    a, b, _ = _seed()
    traversed = _run(
        lambda session: _call(
            session, "traverse", {"start_id": a.id, "edge_types": ["supports"], "depth": 2}
        )
    )
    assert {node["id"] for node in traversed.structuredContent["nodes"]} == {a.id, b.id}

    path = _run(lambda session: _call(session, "find_path", {"a": a.id, "b": b.id}))
    assert path.structuredContent["found"] is True
    assert path.structuredContent["hops"] == 1


def test_list_types_and_get_schema(fresh_db):
    types = _run(lambda session: _call(session, "list_types"))
    assert any(t["name"] == "concept" for t in types.structuredContent["node_types"])

    schema = _run(lambda session: _call(session, "get_schema", {"type": "supports"}))
    assert schema.structuredContent["inverse_name"] == "supported_by"


def test_history_and_diff(fresh_db):
    note = service.create_node(type="note", title="Draft", content="v1 body", principal=owner())
    service.update_node(note.id, content="v2 body", principal=owner())

    history = _run(lambda session: _call(session, "history", {"node_id": note.id}))
    versions = history.structuredContent["result"]
    assert [v["content"] for v in versions] == ["v1 body", "v2 body"]

    diff = _run(
        lambda session: _call(session, "diff", {"a": versions[0]["id"], "b": versions[1]["id"]})
    )
    assert diff.structuredContent["changed_fields"] == ["content"]
    assert "+v2 body" in diff.structuredContent["diff"]


# ── The proposal lifecycle: the agent proposes, the human alone reviews ───────


def test_agent_proposes_over_mcp_and_only_the_human_can_review(fresh_db):
    note = service.create_node(
        type="note",
        title="Original",
        content="original body",
        principal=owner(),
    )

    async def scenario(session):
        first = await _call(session, "update_node", {"id": note.id, "content": "accepted body"})
        second = await _call(session, "update_node", {"id": note.id, "content": "rejected body"})
        return first.structuredContent, second.structuredContent

    keep, drop = _run(scenario)
    # Neither proposal touched the node — the agent has no way to land them.
    assert service.get_node(note.id, principal=owner()).content == "original body"

    # The agent's own principal cannot review either proposal, even its own:
    # suggest covers proposing, never accepting — refusals land per item.
    refused = service.accept_proposals([str(keep["id"])], principal=agent(AGENT))
    assert refused.transitioned == [] and len(refused.failed) == 1
    refused = service.reject_proposals([str(drop["id"])], reason="mine", principal=agent(AGENT))
    assert refused.transitioned == [] and len(refused.failed) == 1

    # The human works the queue out of band (CLI / review API).
    service.accept_proposals([str(keep["id"])], principal=owner())
    service.reject_proposals([str(drop["id"])], reason="not good enough", principal=owner())
    assert service.get_node(note.id, principal=owner()).content == "accepted body"
    versions = service.history(note.id, principal=owner())
    assert [v.state for v in versions] == ["applied", "applied", "archived"]


def test_agent_wikilinks_over_mcp_stay_proposed(fresh_db):
    """A create_node over MCP must not attach a live edge to a human's node."""
    target = service.create_node(type="concept", title="Human Concept", principal=owner())

    async def scenario(session):
        result = await _call(
            session,
            "create_node",
            {"type": "note", "title": "Bot note", "content": "See [[Human Concept]]."},
        )
        return result.structuredContent

    node = _run(scenario)
    assert node["state"] == "proposed"
    assert (
        service.list_edges(node_id=target.id, type="mentions", state="active", principal=owner())
        == []
    )
    (pending,) = service.list_edges(
        node_id=target.id, type="mentions", state="proposed", principal=owner()
    )
    assert pending.created_by == AGENT

    # The human accepting the node is what brings the edge to life.
    service.accept_proposals([node["id"]], principal=owner())
    (live,) = service.list_edges(
        node_id=target.id, type="mentions", state="active", principal=owner()
    )
    assert live.id == pending.id


# ── get_asset: the §5.7 binary policy (renditions only, never originals) ──────


def _register_png(tmp_path, size=(2000, 1000)):
    """Register a PNG and describe it in `main`, so an agent can reach it.

    An asset is as reachable as its `asset_ref` nodes (Phase 4 note 01, D1),
    so registering bytes alone leaves nothing for the MCP surface to fetch.
    """
    source = tmp_path / "picture.png"
    Image.new("RGB", size, (30, 120, 200)).save(source)
    asset = assets.register_asset(source)
    _describe(asset)
    return asset, source


def _describe(asset):
    """The `asset_ref` node ingestion will write; here the owner stands in for it."""
    return service.create_node(
        type="asset_ref",
        title=asset.original_name or asset.hash[:8],
        props={"asset_hash": asset.hash},
        principal=owner(),
    )


def test_get_asset_returns_metadata_and_a_preview_image_block(fresh_db, tmp_path):
    asset, source = _register_png(tmp_path)

    result = _run(lambda session: _call(session, "get_asset", {"id_or_hash": asset.hash}))
    assert not result.isError
    text_block, image_block = result.content
    assert text_block.type == "text"
    assert image_block.type == "image"
    assert image_block.mimeType == "image/webp"

    metadata = json.loads(text_block.text)
    assert metadata["asset"]["hash"] == asset.hash
    assert metadata["rendition"]["profile"] == "preview"

    # The image block is the derived preview, never the original binary.
    payload = base64.b64decode(image_block.data)
    assert payload != source.read_bytes()
    with Image.open(io.BytesIO(payload)) as decoded:
        assert decoded.format == "WEBP"
        assert decoded.size == (1024, 512)


def test_get_asset_thumb_profile(fresh_db, tmp_path):
    asset, _ = _register_png(tmp_path)
    result = _run(
        lambda session: _call(
            session, "get_asset", {"id_or_hash": asset.hash, "rendition": "thumb"}
        )
    )
    image_block = result.content[1]
    with Image.open(io.BytesIO(base64.b64decode(image_block.data))) as decoded:
        assert decoded.size == (256, 128)


def test_get_asset_non_image_returns_metadata_only(fresh_db, tmp_path):
    text_file = tmp_path / "notes.txt"
    text_file.write_text("plain text")
    asset = assets.register_asset(text_file)
    _describe(asset)

    result = _run(lambda session: _call(session, "get_asset", {"id_or_hash": asset.hash}))
    assert not result.isError
    assert len(result.content) == 1
    metadata = json.loads(result.content[0].text)
    assert metadata["asset"]["mime"] == "text/plain"
    assert metadata["rendition"] is None


def test_get_asset_never_serves_the_original(fresh_db, tmp_path):
    asset, _ = _register_png(tmp_path)
    for profile in ("full", "page:1", "original"):
        result = _run(
            lambda session, profile=profile: _call(
                session, "get_asset", {"id_or_hash": asset.hash, "rendition": profile}
            )
        )
        assert result.isError


# ── Error surfacing ───────────────────────────────────────────────────────────


def test_service_errors_surface_as_tool_errors(fresh_db):
    result = _run(lambda session: _call(session, "get_node", {"id": "missing"}))
    assert result.isError
    assert "node not found" in result.content[0].text


# ── get_asset: extracted text and page rasters (Phase 4) ─────────────────────


def _ingest_text(tmp_path, body="Vercingetorix basin hydrology", name="hydrology.txt"):
    """Ingest a text file as the owner, so its describing node is *active*.

    An asset is only reachable through an active `asset_ref` (note 01 D1), and
    an agent holding `suggest` proposes one — so a reader test seeds through the
    human, exactly as `_describe` does for a bare registration.
    """
    source = tmp_path / name
    source.write_text(body, encoding="utf-8")
    return ingest.ingest_file(source, principal=owner())


def test_get_asset_returns_the_extracted_text_of_an_ingested_document(fresh_db, tmp_path):
    """The docstring used to say extraction "lands with the Phase-4 pipeline"."""
    ingested = _ingest_text(tmp_path)

    result = _run(lambda session: _call(session, "get_asset", {"id_or_hash": ingested.asset.hash}))

    assert not result.isError
    assert len(result.content) == 1  # a text file has no rendition
    metadata = json.loads(result.content[0].text)
    assert "Vercingetorix basin hydrology" in metadata["extracted_text"]
    assert metadata["extracted_chars"] == len(metadata["extracted_text"])
    assert metadata["text_truncated"] is False
    assert metadata["rendition"] is None


def test_get_asset_reports_a_truncated_text_rather_than_clipping_it_silently(
    fresh_db, tmp_path, monkeypatch
):
    """The cap is the source node's own; `extracted_chars` still tells the truth."""
    monkeypatch.setattr(mcp_server, "MAX_EXTRACTED_TEXT_CHARS", 10)
    ingested = _ingest_text(tmp_path, body="w" * 500, name="long.txt")

    result = _run(lambda session: _call(session, "get_asset", {"id_or_hash": ingested.asset.hash}))

    metadata = json.loads(result.content[0].text)
    assert metadata["extracted_text"] == "w" * 10
    assert metadata["extracted_chars"] == 500
    assert metadata["text_truncated"] is True


def test_get_asset_says_so_when_nothing_extracted(fresh_db, tmp_path):
    """No handler read the bytes — null text, not an empty string pretending."""
    source = tmp_path / "mystery.bin"
    source.write_bytes(b"\x00\x01\x02\x03")
    asset = assets.register_asset(source)
    _describe(asset)

    result = _run(lambda session: _call(session, "get_asset", {"id_or_hash": asset.hash}))

    metadata = json.loads(result.content[0].text)
    assert metadata["extracted_text"] is None
    assert metadata["extracted_chars"] == 0


@pytest.mark.skipif(
    importlib.util.find_spec("pypdfium2") is None, reason="the pdf extra is not installed"
)
def test_get_asset_serves_a_page_raster_for_a_pdf(fresh_db):
    """`page:<n>` is how an agent *looks at* a document — still never the original."""
    asset = assets.register_asset(FIXTURE_PDF)
    _describe(asset)

    result = _run(
        lambda session: _call(
            session, "get_asset", {"id_or_hash": asset.hash, "rendition": "page:1"}
        )
    )

    assert not result.isError
    text_block, image_block = result.content
    assert json.loads(text_block.text)["rendition"]["profile"] == "page:1"
    assert image_block.mimeType == "image/webp"
    payload = base64.b64decode(image_block.data)
    assert payload != FIXTURE_PDF.read_bytes()
    with Image.open(io.BytesIO(payload)) as decoded:
        assert decoded.format == "WEBP"


@pytest.mark.parametrize("profile", ["full", "original", "page:0", "page:01", "PAGE:1"])
def test_get_asset_refuses_an_unknown_profile(fresh_db, tmp_path, profile):
    """One spelling per rendition, and no name that could mean the original."""
    asset, _ = _register_png(tmp_path)

    result = _run(
        lambda session: _call(
            session, "get_asset", {"id_or_hash": asset.hash, "rendition": profile}
        )
    )

    assert result.isError
    assert "originals never" in result.content[0].text


def test_get_asset_refuses_a_page_of_something_that_is_not_a_pdf(fresh_db, tmp_path):
    """A named page that cannot be rendered is a failed request, not a null rendition."""
    asset, _ = _register_png(tmp_path)

    result = _run(
        lambda session: _call(
            session, "get_asset", {"id_or_hash": asset.hash, "rendition": "page:1"}
        )
    )

    assert result.isError
    assert "PDF" in result.content[0].text


# ── Ingestion over MCP: by reference, additive, and confined by the grant ─────


class _CannedHandler(BaseHTTPRequestHandler):
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
    """A loopback HTTP server serving one canned body — the suite never leaves the machine."""
    server = HTTPServer(("127.0.0.1", 0), _CannedHandler)
    server.canned = (b"", "text/plain")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _url(server, path: str) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}{path}"


def test_ingest_file_takes_a_local_path_and_writes_the_subgraph(fresh_db, tmp_path):
    """Ingestion by reference: the path crosses MCP, the bytes never do."""
    source = tmp_path / "hydrology.txt"
    source.write_text("Vercingetorix basin hydrology", encoding="utf-8")

    result = _run(lambda session: _call(session, "ingest_file", {"path_or_url": str(source)}))

    assert not result.isError
    out = result.structuredContent
    assert out["created"] is True
    assert out["extraction"]["handler"] == "text"
    assert out["asset_ref"]["props"]["asset_hash"] == out["asset"]["hash"]
    assert "hydrology" in out["source"]["content"]
    (edge,) = out["edges"]
    assert (edge["src_id"], edge["dst_id"], edge["type"]) == (
        out["source"]["id"],
        out["asset_ref"]["id"],
        "derived_from",
    )


def test_ingest_file_routes_an_http_url_to_the_fetch_path(fresh_db, fixture_server):
    """Only an http/https value leaves the filesystem; anything else is a local path."""
    fixture_server.canned = (
        b"<html><body><p>Basin hydrology</p></body></html>",
        "text/html; charset=utf-8",
    )

    result = _run(
        lambda session: _call(
            session, "ingest_file", {"path_or_url": _url(fixture_server, "/article")}
        )
    )

    out = result.structuredContent
    assert out["extraction"]["handler"] == "html"
    assert out["source"]["content"] == "Basin hydrology"
    assert out["asset_ref"]["props"]["url"].endswith("/article")


def test_ingest_url_fetches_and_records_its_provenance(fresh_db, fixture_server):
    fixture_server.canned = (b"<p>Basin hydrology</p>", "text/html")

    result = _run(
        lambda session: _call(session, "ingest_url", {"url": _url(fixture_server, "/paper")})
    )

    out = result.structuredContent
    assert out["source"]["props"]["url"].endswith("/paper")
    assert out["extraction"]["chars"] > 0


def test_ingest_url_refuses_a_scheme_it_will_not_fetch(fresh_db):
    result = _run(lambda session: _call(session, "ingest_url", {"url": "file:///etc/passwd"}))

    assert result.isError
    assert "ingest_url takes" in result.content[0].text


def test_a_suggest_agent_gets_a_proposed_subgraph_and_the_tool_does_not_claim_otherwise(
    fresh_db, tmp_path
):
    """Ingestion adds no authority of its own — the landing state is the grant's."""
    source = tmp_path / "proposal.txt"
    source.write_text("proposal", encoding="utf-8")

    out = _run(
        lambda session: _call(session, "ingest_file", {"path_or_url": str(source)})
    ).structuredContent

    assert out["asset_ref"]["state"] == "proposed"
    assert out["source"]["state"] == "proposed"
    assert out["edges"][0]["state"] == "proposed"
    assert out["asset_ref"]["created_by"] == AGENT

    # And the description names the grant rather than promising `proposed` (Q13).
    descriptions = {
        tool.name: tool.description for tool in _run(lambda session: session.list_tools()).tools
    }
    for name in INGEST_TOOLS:
        assert "suggest" in descriptions[name] and "edit" in descriptions[name], name


def test_re_ingesting_the_same_file_adds_nothing(fresh_db, tmp_path):
    """The idempotency gate is what makes the additive annotation true on a re-run."""
    source = tmp_path / "twice.txt"
    source.write_text("marginalia", encoding="utf-8")

    first = _run(
        lambda session: _call(session, "ingest_file", {"path_or_url": str(source)})
    ).structuredContent
    second = _run(
        lambda session: _call(session, "ingest_file", {"path_or_url": str(source)})
    ).structuredContent

    assert second["created"] is False
    assert second["asset_ref"]["id"] == first["asset_ref"]["id"]
    assert len(service.list_nodes(type="asset_ref", principal=owner())) == 1


def test_ingest_file_refuses_a_path_that_is_not_a_file(fresh_db, tmp_path):
    result = _run(lambda session: _call(session, "ingest_file", {"path_or_url": str(tmp_path)}))

    assert result.isError
    assert "not a file" in result.content[0].text


# ── The escape hatch: a logged, single-use URL to the original bytes ──────────


def test_get_download_url_mints_a_single_use_url_and_logs_the_mint(fresh_db, tmp_path):
    asset, source = _register_png(tmp_path)

    grant = _run(
        lambda session: _call(session, "get_download_url", {"id_or_hash": asset.hash})
    ).structuredContent

    assert grant["kind"] == "download"
    assert grant["asset_hash"] == asset.hash
    assert grant["url"] == f"{urls.public_base_url()}/api/download/{grant['token']}"

    mints = [event for event in service.list_events(owner()) if event.op == "asset.download_url"]
    assert [event.actor for event in mints] == [AGENT]
    assert grant["token"] not in json.dumps([event.payload for event in mints])

    # Single use: the first redemption spends it, the second is refused.
    assert urls.consume(grant["token"], kind="download")["asset_hash"] == asset.hash
    with pytest.raises(urls.TokenInvalid):
        urls.consume(grant["token"], kind="download")
    assert source.exists()  # the bytes never crossed MCP to get here


def test_get_download_url_cannot_be_turned_into_a_permanent_capability(fresh_db, tmp_path):
    asset, _ = _register_png(tmp_path)

    result = _run(
        lambda session: _call(
            session,
            "get_download_url",
            {"id_or_hash": asset.hash, "ttl_seconds": urls.MAX_TTL_SECONDS + 1},
        )
    )

    assert result.isError
    assert "ttl_seconds" in result.content[0].text


def test_request_upload_url_hands_out_exactly_one_use(fresh_db):
    result = _run(
        lambda session: _call(
            session,
            "request_upload_url",
            {"name": "scan.pdf", "mime": "application/pdf", "size": 4096},
        )
    ).structuredContent

    assert result["asset"] is None
    grant = result["grant"]
    assert grant["max_bytes"] == 4096
    assert grant["url"].endswith(f"/api/uploads/{grant['token']}")

    assert urls.consume(grant["token"], kind="upload")["original_name"] == "scan.pdf"
    with pytest.raises(urls.TokenInvalid):
        urls.consume(grant["token"], kind="upload")


def test_request_upload_url_dedups_a_declared_hash_without_moving_bytes(fresh_db, tmp_path):
    """Design §5.7 rule 4: the declared hash is what makes the dedup instant."""
    asset, _ = _register_png(tmp_path)

    result = _run(
        lambda session: _call(
            session,
            "request_upload_url",
            {
                "name": "picture.png",
                "mime": "image/png",
                "size": asset.size_bytes,
                "sha256": asset.hash,
            },
        )
    ).structuredContent

    assert result["grant"] is None
    assert result["asset"]["hash"] == asset.hash


# ── Scope: a grant is the whole of an agent's reach, ingestion included ───────


def test_an_agent_cannot_ingest_into_a_space_it_holds_nothing_on(fresh_db, tmp_path):
    """An ungranted space and a nonexistent one answer identically (Q13 S3)."""
    seed_space("research")
    source = tmp_path / "scoped.txt"
    source.write_text("scoped", encoding="utf-8")

    result = _run(
        lambda session: _call(
            session, "ingest_file", {"path_or_url": str(source), "space": "research"}
        )
    )

    assert result.isError
    assert "unknown space" in result.content[0].text
    assert service.list_nodes(type="asset_ref", principal=owner()) == []


def test_a_read_only_agent_cannot_ingest_at_all(fresh_db, tmp_path):
    source = tmp_path / "scoped.txt"
    source.write_text("scoped", encoding="utf-8")

    result = _run(
        lambda session: _call(session, "ingest_file", {"path_or_url": str(source)}),
        actor="agent:reader",
        token="ndm_test_token_for_the_reader",
        grants={"meta": "read", "main": "read"},
    )

    assert result.isError
    assert service.list_nodes(type="asset_ref", principal=owner()) == []


def test_an_agent_cannot_reach_bytes_described_only_in_a_space_it_cannot_read(fresh_db, tmp_path):
    """Neither the metadata nor a download URL: the describing node carries both."""
    seed_space("research")
    source = tmp_path / "confidential.txt"
    source.write_text("confidential", encoding="utf-8")
    ingested = ingest.ingest_file(source, space="research", principal=owner())

    fetched = _run(lambda session: _call(session, "get_asset", {"id_or_hash": ingested.asset.hash}))
    minted = _run(
        lambda session: _call(session, "get_download_url", {"id_or_hash": ingested.asset.hash})
    )

    assert fetched.isError and "asset not found" in fetched.content[0].text
    assert minted.isError and "asset not found" in minted.content[0].text
    # Refused means refused: no capability was written either.
    logged = [event.op for event in service.list_events(owner()) if event.op.startswith("asset.")]
    assert logged == ["asset.ingest"]
