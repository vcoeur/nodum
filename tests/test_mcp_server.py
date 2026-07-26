"""MCP server tests: every registered tool round-trips through the MCP layer.

The server is exercised in-process over memory streams
(``create_connected_server_and_client_session``) — the same handlers stdio
clients reach, no subprocess needed.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json

import pytest
from helpers import agent, owner
from mcp.shared.memory import create_connected_server_and_client_session
from PIL import Image

from nodum import assets, auth, service
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


def _run(fn):
    """Run an async MCP interaction against a fresh server bound to AGENT."""

    async def runner():
        agent(AGENT, token=TOKEN)  # seed the account with a verifiable token
        server = create_server(token=TOKEN)
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
    source = tmp_path / "picture.png"
    Image.new("RGB", size, (30, 120, 200)).save(source)
    return assets.register_asset(source), source


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
