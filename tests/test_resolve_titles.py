"""Exact title resolution: the read side of ``suggest-links``.

Where ``suggest_links`` answers a prefix typed so far, ``resolve_titles``
answers whole ``[[wikilink]]`` titles exactly — one call for a whole rendered
document. This file pins the semantics that make it safe to navigate a click
on: case- and normalisation-insensitive matching over non-archived nodes, a
space preference for breaking ties, and never a leak of a node (or a space)
the caller cannot see.
"""

from __future__ import annotations

import asyncio
import json
import unicodedata

import httpx
import pytest
from helpers import agent, owner
from typer.testing import CliRunner

from nodum import http_api, service
from nodum.cli import app

runner = CliRunner()

OWNER_PASSWORD = "correct horse battery"
BASE_URL = "http://127.0.0.1:8600"
CLIENT_HEADERS = {http_api.CLIENT_HEADER: "nodum-tests"}


def _library():
    """Titles exercising exact match, case folding, and state filtering."""
    created = {}
    created["graph"] = service.create_node(type="concept", title="Graph Theory", principal=owner())
    created["éco"] = service.create_node(type="note", title="École normale", principal=owner())
    return created


def _outcomes(resolutions):
    return {entry.title: entry.outcome for entry in resolutions}


def _resolved(resolutions, title):
    (entry,) = [entry for entry in resolutions if entry.title == title]
    assert entry.outcome == "resolved"
    return entry


# ── Matching semantics ───────────────────────────────────────────────────────


def test_exact_match_resolves_with_id_and_space(fresh_db):
    _library()
    (entry,) = service.resolve_titles(["Graph Theory"], principal=owner())
    assert entry.outcome == "resolved"
    assert entry.node_id is not None
    assert entry.space_id == "main"


def test_matching_folds_case_and_unicode_normalisation(fresh_db):
    _library()
    # Case folding happens in Python (`str.casefold`), not SQL: `lower()`
    # folds ASCII only, and case folding can itself denormalise (ß → ss).
    assert _resolved(service.resolve_titles(["graph theory"], principal=owner()), "graph theory")
    assert _resolved(service.resolve_titles(["GRAPH THEORY"], principal=owner()), "GRAPH THEORY")
    service.create_node(type="concept", title="École", principal=owner())
    assert _resolved(service.resolve_titles(["ÉCOLE"], principal=owner()), "ÉCOLE")
    assert _resolved(service.resolve_titles(["école"], principal=owner()), "école")

    # An accented letter is one code point or two, depending on who typed it —
    # the same normalisation suggest_links applies on both sides.
    decomposed = unicodedata.normalize("NFD", "École")
    assert _resolved(service.resolve_titles([decomposed], principal=owner()), decomposed)


def test_exact_never_prefix(fresh_db):
    _library()
    outcomes = _outcomes(service.resolve_titles(["Graph"], principal=owner()))
    assert outcomes == {"Graph": "not-found"}


def test_archived_excluded_and_proposed_included(fresh_db):
    archived = service.create_node(type="note", title="pending idea", principal=owner())
    service.transition(archived.id, "archive", principal=owner())
    outcomes = _outcomes(service.resolve_titles(["pending idea"], principal=owner()))
    assert outcomes == {"pending idea": "not-found"}
    # An agent's write lands `proposed` — the same non-archived states
    # suggest_links draws from, so a pending node is still a link target.
    proposed = service.create_node(type="note", title="draft idea", principal=agent("drafter"))
    assert proposed.state == "proposed"
    assert (
        _resolved(service.resolve_titles(["draft idea"], principal=owner()), "draft idea").node_id
        == proposed.id
    )


def test_ambiguous_when_several_nodes_share_a_title(fresh_db):
    service.create_node(type="note", title="web auth", principal=owner())
    service.create_node(type="note", title="Web Auth", principal=owner())
    (entry,) = service.resolve_titles(["web auth"], principal=owner())
    assert entry.outcome == "ambiguous"
    assert entry.node_id is None
    assert entry.space_id is None


def test_resolves_a_list_and_keeps_request_order(fresh_db):
    library = _library()
    resolutions = service.resolve_titles(
        ["Graph Theory", "missing", "École normale"], principal=owner()
    )
    assert [entry.title for entry in resolutions] == ["Graph Theory", "missing", "École normale"]
    assert resolutions[0].node_id == library["graph"].id
    assert resolutions[1].outcome == "not-found"
    assert resolutions[2].node_id == library["éco"].id


# ── The space preference ─────────────────────────────────────────────────────


def _two_space_library():
    service.create_space("research", principal=owner())
    main_note = service.create_node(type="note", title="duplicate", principal=owner())
    research_note = service.create_node(
        type="note", title="duplicate", space="research", principal=owner()
    )
    only_research = service.create_node(
        type="note", title="only here", space="research", principal=owner()
    )
    return main_note, research_note, only_research


def test_space_preference_breaks_the_tie(fresh_db):
    main_note, research_note, _ = _two_space_library()
    assert (
        _resolved(
            service.resolve_titles(["duplicate"], space="main", principal=owner()), "duplicate"
        ).node_id
        == main_note.id
    )
    assert (
        _resolved(
            service.resolve_titles(["duplicate"], space="research", principal=owner()), "duplicate"
        ).node_id
        == research_note.id
    )


def test_space_preference_only_prefers_never_hides(fresh_db):
    _, _, only_research = _two_space_library()
    # The title exists only outside the preferred space, so the preference
    # falls back to every space in scope rather than answering not-found.
    assert (
        _resolved(
            service.resolve_titles(["only here"], space="main", principal=owner()), "only here"
        ).node_id
        == only_research.id
    )


def test_ambiguous_without_a_preference_stays_ambiguous(fresh_db):
    _two_space_library()
    assert service.resolve_titles(["duplicate"], principal=owner())[0].outcome == "ambiguous"
    # A preference naming a space that holds no match changes nothing.
    service.create_space("elsewhere", principal=owner())
    (entry,) = service.resolve_titles(["duplicate"], space="elsewhere", principal=owner())
    assert entry.outcome == "ambiguous"


def test_a_preference_space_the_principal_cannot_read_does_not_resolve(fresh_db):
    service.create_space("research", principal=owner())
    outsider = agent("outsider", grants={"meta": "read"})
    with pytest.raises(service.TypeNotFound, match="unknown space: research"):
        service.resolve_titles(["anything"], space="research", principal=outsider)


# ── No existence oracle ──────────────────────────────────────────────────────


def test_a_node_outside_the_read_set_is_not_found_never_ambiguous(fresh_db):
    """The grant model's answer, verbatim: an unreadable node does not exist.

    If resolution reported ``ambiguous`` for a title whose only copies sit in
    a space the caller cannot read, it would probe that space's existence
    through the back door — the same leak ``_resolve_wikilink`` is written to
    avoid on the write side.
    """
    service.create_space("research", principal=owner())
    service.create_node(type="note", title="secret", space="research", principal=owner())
    service.create_node(type="note", title="secret", space="research", principal=owner())
    main_public = service.create_node(type="note", title="public", principal=owner())
    service.create_node(type="note", title="public", space="research", principal=owner())
    # Reads `main` (and meta) but not `research`.
    outsider = agent("outsider", grants={"meta": "read", "main": "read"})
    # Both copies are invisible: not-found, never ambiguous.
    (entry,) = service.resolve_titles(["secret"], principal=outsider)
    assert entry.outcome == "not-found"
    # One copy is readable: the title resolves to it, never ambiguous about
    # the copy it cannot see.
    (entry,) = service.resolve_titles(["public"], principal=outsider)
    assert entry.outcome == "resolved"
    assert entry.node_id == main_public.id


def test_a_granted_read_set_resolves_its_own_nodes(fresh_db):
    """The positive control: visibility, not an empty read set."""
    service.create_space("research", principal=owner())
    research_node_id = service.resolve_space_id("research", principal=owner())
    note = service.create_node(type="note", title="visible", space="research", principal=owner())
    insider = agent("insider", grants={"meta": "read", research_node_id: "read"})
    assert (
        _resolved(service.resolve_titles(["visible"], principal=insider), "visible").node_id
        == note.id
    )


# ── CLI and HTTP adapters ────────────────────────────────────────────────────


def test_the_cli_prints_one_envelope_per_title(fresh_db):
    _library()
    result = runner.invoke(app, ["resolve-titles", "Graph Theory", "missing", "--as", "owner"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["count"] == 2
    assert payload["resolutions"][0]["outcome"] == "resolved"
    assert payload["resolutions"][0]["space_id"] == "main"
    assert payload["resolutions"][1] == {
        "title": "missing",
        "outcome": "not-found",
        "node_id": None,
        "space_id": None,
    }


def test_the_http_route_is_byte_identical_to_the_cli(fresh_db):
    """Both adapters render one envelope, so their JSON cannot drift."""
    _library()
    cli = runner.invoke(app, ["resolve-titles", "Graph Theory", "--as", "owner"])
    assert cli.exit_code == 0, cli.output

    async def run() -> httpx.Response:
        app_instance = http_api.create_app()
        service.set_human_password("owner", OWNER_PASSWORD, principal=owner())
        transport = httpx.ASGITransport(app=app_instance, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as web:
            login = await web.post(
                "/api/login",
                json={"name": "owner", "password": OWNER_PASSWORD},
                headers={"Content-Type": "application/json", http_api.CLIENT_HEADER: "tests"},
            )
            assert login.status_code == 200, login.text
            cookie = login.cookies[http_api.SESSION_COOKIE]
            return await web.get(
                "/api/nodes/resolve?titles=Graph%20Theory",
                headers={"Cookie": f"{http_api.SESSION_COOKIE}={cookie}"},
            )

    response = asyncio.run(run())
    assert response.status_code == 200, response.text
    assert json.dumps(response.json(), sort_keys=True) == json.dumps(
        json.loads(cli.output), sort_keys=True
    )


def test_the_http_route_takes_repeated_titles_and_a_space(fresh_db):
    _, research_note, only_research = _two_space_library()
    response = _http_get("/api/nodes/resolve?titles=duplicate&titles=only+here&space=research")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["count"] == 2
    by_title = {entry["title"]: entry for entry in payload["resolutions"]}
    assert by_title["duplicate"]["outcome"] == "resolved"
    assert by_title["duplicate"]["node_id"] == research_note.id
    assert by_title["only here"]["node_id"] == only_research.id


def test_missing_titles_is_a_400(fresh_db):
    response = _http_get("/api/nodes/resolve")
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "ValueError"


def test_the_resolve_route_needs_a_session_like_every_other_api_route(fresh_db):
    app_instance = http_api.create_app()

    async def call() -> httpx.Response:
        transport = httpx.ASGITransport(app=app_instance, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as web:
            return await web.get("/api/nodes/resolve?titles=anything")

    response = asyncio.run(call())
    assert response.status_code == 401


def _http_get(path: str) -> httpx.Response:
    """One authenticated GET over the fresh database's app."""

    async def call() -> httpx.Response:
        app_instance = http_api.create_app()
        service.set_human_password("owner", OWNER_PASSWORD, principal=owner())
        transport = httpx.ASGITransport(app=app_instance, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as web:
            login = await web.post(
                "/api/login",
                json={"name": "owner", "password": OWNER_PASSWORD},
                headers={"Content-Type": "application/json", http_api.CLIENT_HEADER: "tests"},
            )
            assert login.status_code == 200, login.text
            cookie = login.cookies[http_api.SESSION_COOKIE]
            return await web.get(
                path,
                headers={"Cookie": f"{http_api.SESSION_COOKIE}={cookie}"},
            )

    return asyncio.run(call())
