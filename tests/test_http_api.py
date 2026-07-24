"""HTTP API tests: the ASGI app driven in-process, no socket ever bound.

Mirrors the MCP session pattern — one ``asyncio.run`` per interaction over an
``httpx.ASGITransport``, so the tests themselves stay synchronous and reach the
exact handlers a browser would.

The heart of the file is the human-only guarantee. Those tests assert the
*property* ("no write this surface makes is attributable to anything but the
human"), not the mechanism that currently delivers it: three of them are
structural assertions over the app's real route table and this module's own
AST, so an endpoint added later is covered without anyone remembering to add
it to a list.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import io
import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from PIL import Image
from typer.testing import CliRunner

from nodum import assets, cli, http_api, service

AGENT = "agent:researcher"
BASE_URL = "http://nodum.test"

runner = CliRunner()


class Client:
    """Synchronous in-process driver over the ASGI app.

    ``raise_app_exceptions=False`` keeps an unhandled exception inside the
    response cycle — Starlette's ``ServerErrorMiddleware`` re-raises after
    sending the 500 body so a real server can log the traceback, and the test
    wants to inspect that body.
    """

    def __init__(self, app, token: str | None = None) -> None:
        self.app = app
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Issue one request and return the response."""
        headers = {**self.headers, **kwargs.pop("headers", {})}

        async def run() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
                return await client.request(method, path, headers=headers, **kwargs)

        return asyncio.run(run())

    def get(self, path: str, **kwargs) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> httpx.Response:
        return self.request("POST", path, **kwargs)

    def patch(self, path: str, **kwargs) -> httpx.Response:
        return self.request("PATCH", path, **kwargs)

    def put(self, path: str, **kwargs) -> httpx.Response:
        return self.request("PUT", path, **kwargs)


@pytest.fixture()
def client(fresh_db):
    """A client over an app bound to the fresh test database (no auth)."""
    return Client(http_api.create_app())


@pytest.fixture()
def bundle(tmp_path, monkeypatch):
    """Point the static mount at a fake built bundle and return its root."""
    root = tmp_path / "_web"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>nodum SPA</title>", encoding="utf-8")
    (root / "assets" / "app.js").write_text("console.log('nodum')", encoding="utf-8")
    monkeypatch.setattr(http_api, "WEB_ROOT", root)
    return root


def _ok(response: httpx.Response) -> dict:
    """Assert a 2xx and return the parsed body."""
    assert response.status_code == 200, response.text
    return response.json()


def _png_bytes() -> bytes:
    """A tiny valid PNG, for the asset-upload and rendition paths."""
    buffer = io.BytesIO()
    Image.new("RGB", (12, 8), "purple").save(buffer, "PNG")
    return buffer.getvalue()


def _events(op: str | None = None) -> list:
    """Read the event log, newest first, optionally filtered by op."""
    rows = service.list_events(limit=500)
    return [row for row in rows if op is None or row.op == op]


# ── 1. Envelope parity: the CLI and the API emit the same bytes ───────────────


def test_node_get_is_byte_identical_to_the_cli(client, fresh_db):
    """Literal byte parity, multibyte content included (both sides keep UTF-8)."""
    node = service.create_node(
        type="note",
        title="École — théorie des graphes",
        content="Un résumé « précis » avec des ✓ et une flèche →.",
        props={"clé": "valeur"},
    )
    result = runner.invoke(cli.app, ["node", "get", node.id])
    assert result.exit_code == 0, result.output

    response = client.get(f"/api/nodes/{node.id}")
    assert response.status_code == 200
    assert response.content == result.stdout.encode("utf-8")
    assert "École" in response.text  # not École: ensure_ascii=False on both sides


@pytest.mark.parametrize(
    ("http_path", "cli_args"),
    [
        ("/api/nodes", ["node", "list"]),
        ("/api/edges", ["edge", "list"]),
        ("/api/events", ["events"]),
    ],
)
def test_list_envelopes_are_byte_identical_to_the_cli(client, fresh_db, http_path, cli_args):
    service.create_node(type="concept", title="Graph Theory")
    service.create_node(type="note", title="Note", content="about [[Graph Theory]]")

    result = runner.invoke(cli.app, cli_args)
    assert result.exit_code == 0, result.output

    response = client.get(http_path)
    assert response.status_code == 200
    assert response.content == result.stdout.encode("utf-8")
    payload = response.json()
    key = next(name for name in payload if name != "count")
    assert payload["count"] == len(payload[key])


def test_every_list_endpoint_uses_the_named_key_plus_count(client, fresh_db):
    node = service.create_node(type="note", title="Envelope", content="x")
    service.create_node(type="note", title="Child", parent_id=node.id)
    service.set_policy(AGENT, [{"edge_type": "mentions", "action": "auto_accept"}])
    service.create_node(type="note", title="Proposed", actor=AGENT)

    for path, key in [
        ("/api/nodes", "nodes"),
        (f"/api/nodes/{node.id}/children", "nodes"),
        (f"/api/nodes/{node.id}/history", "versions"),
        ("/api/edges", "edges"),
        ("/api/links/suggest?prefix=", "nodes"),
        ("/api/review/queue", "proposals"),
        ("/api/policies", "policies"),
        ("/api/assets", "assets"),
        ("/api/events", "events"),
    ]:
        payload = _ok(client.get(path))
        assert set(payload) == {key, "count"}, path
        assert payload["count"] == len(payload[key]), path


# ── 2. Error taxonomy ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("exception", "status"),
    sorted(http_api.EXCEPTION_STATUS.items(), key=lambda item: item[0].__name__),
)
def test_every_mapped_exception_becomes_its_status_and_envelope(
    client, fresh_db, monkeypatch, exception, status
):
    """Table-driven over EXCEPTION_STATUS itself, so a new mapping is covered.

    A read endpoint stands in for "any handler": the mapping is installed on
    the app, not per route. Two rows (``ReviewNotPermitted``, the locked
    database) cannot be provoked from this surface at all — the first because
    the actor is always the human, the second because it needs a competing
    writer — which is exactly why they are raised here rather than staged.
    """

    def raise_it(*args, **kwargs):
        raise exception("boom")

    monkeypatch.setattr(service, "list_types", raise_it)
    response = client.get("/api/types")

    assert response.status_code == status
    body = response.json()
    assert body["error"]["type"] == exception.__name__
    expected = "database error: boom" if issubclass(exception, sqlite3.Error) else "boom"
    assert body["error"]["message"] == expected


def test_an_unmapped_exception_is_a_generic_500(client, fresh_db, monkeypatch):
    def raise_it(*args, **kwargs):
        raise RuntimeError("kaboom: /home/alice/secret")

    monkeypatch.setattr(service, "list_types", raise_it)
    response = client.get("/api/types")

    assert response.status_code == 500
    assert response.json() == {"error": http_api.INTERNAL_ERROR}
    assert "kaboom" not in response.text
    assert "Traceback" not in response.text


def test_real_failures_carry_the_same_taxonomy(client, fresh_db):
    assert client.get("/api/nodes/does-not-exist").status_code == 404
    assert client.get("/api/nodes/does-not-exist").json()["error"]["type"] == "NodeNotFound"
    assert client.get("/api/schema/nope").status_code == 404
    assert client.get("/api/policies/agent:nobody").status_code == 404
    assert client.get("/api/assets/deadbeef").status_code == 404
    # A bad enum value is the caller's fault, not a missing record.
    assert client.get("/api/nodes?state=bogus").status_code == 400
    # Archiving twice: the second transition is impossible from `archived`.
    node = service.create_node(type="note", title="Twice")
    assert client.post(f"/api/nodes/{node.id}/archive").status_code == 200
    repeat = client.post(f"/api/nodes/{node.id}/archive")
    assert repeat.status_code == 400
    assert repeat.json()["error"]["type"] == "InvalidTransition"


def test_an_undo_the_graph_has_grown_past_is_a_409(client, fresh_db):
    parent = service.create_node(type="note", title="Parent")
    service.create_node(type="note", title="Child", parent_id=parent.id)
    create_seq = next(
        row.seq for row in _events("node.create") if row.payload["after"]["id"] == parent.id
    )

    response = client.post("/api/undo", json={"seq": create_seq})
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "UndoNotPossible"


def test_unknown_api_routes_are_json_404s_not_the_spa(client, bundle):
    response = client.get("/api/nope/at/all")
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "NotFound"
    assert "nodum SPA" not in response.text


# ── 3. The human-only guarantee, as four properties ───────────────────────────


def _route_endpoints(app) -> list[tuple[str, object]]:
    """Walk the app's real route table and return every (path, endpoint) pair."""
    found: list[tuple[str, object]] = []
    pending = list(app.routes)
    while pending:
        route = pending.pop()
        pending.extend(getattr(route, "routes", None) or [])
        endpoint = getattr(route, "endpoint", None)
        if endpoint is not None:
            found.append((getattr(route, "path", "?"), endpoint))
    return found


def _module_ast() -> ast.Module:
    """Parse the HTTP adapter's own source."""
    return ast.parse(Path(http_api.__file__).read_text(encoding="utf-8"))


def test_no_route_handler_can_read_an_actor_from_a_request(fresh_db):
    """Property 1 (enumerated absence), asserted over the live route table.

    Not a hand-maintained list: the endpoints come from ``app.routes``, so an
    endpoint added tomorrow is walked too. The rule a handler must satisfy is
    the strongest available statically — it may not mention an actor at all,
    which makes reading one from a body, header, or query string impossible.
    The single binding lives in ``_write`` (asserted next), which is not a
    route handler.
    """
    app = http_api.create_app()
    endpoints = _route_endpoints(app)
    # Sanity: the walk really covered the surface it claims to.
    assert len(endpoints) >= 30
    assert {path for path, _ in endpoints} >= {"/healthz", "/api/nodes", "/{path:path}"}

    offenders = [
        path for path, endpoint in endpoints if "actor" in inspect.getsource(endpoint).casefold()
    ]
    assert offenders == [], f"route handlers must never name an actor: {offenders}"


def test_the_actor_is_bound_exactly_once_and_to_the_human_constant():
    """Property 1, second half: one binding site, and it is a constant."""
    bindings = [
        keyword
        for node in ast.walk(_module_ast())
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "actor"
    ]
    assert len(bindings) == 1, "exactly one expression may bind a service actor"
    value = bindings[0].value
    assert isinstance(value, ast.Name) and value.id == "HTTP_ACTOR"
    assert http_api.HTTP_ACTOR == service.ACTOR_HUMAN


def test_no_write_service_function_is_called_outside_the_write_helper():
    """Property 1, third half: a handler cannot reach a write without ``_write``.

    Without this, a new endpoint could call ``service.create_node(...)``
    directly, never mention an actor, and silently write as the service default
    instead of through the one attributed path.
    """
    actor_taking = {
        name
        for name, function in vars(service).items()
        if inspect.isfunction(function)
        and function.__module__ == service.__name__
        and "actor" in inspect.signature(function).parameters
    }
    assert actor_taking >= {
        "create_node",
        "update_node",
        "create_edge",
        "transition",
        "undo",
        "set_policy",
        "accept_proposals",
        "reject_proposals",
        "accept_matching",
        "reject_matching",
    }

    direct = [
        node.func.attr
        for node in ast.walk(_module_ast())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and getattr(node.func.value, "id", None) == "service"
        and node.func.attr in actor_taking
    ]
    assert direct == [], f"these must go through _write(): {sorted(set(direct))}"


def test_the_write_helper_refuses_a_caller_supplied_actor(fresh_db):
    """The backstop: even a wholesale kwargs forward cannot smuggle an identity."""
    with pytest.raises(RuntimeError, match="never takes an actor"):
        http_api._write(service.create_node, type="note", actor=AGENT)


@pytest.mark.parametrize(
    "smuggled",
    [
        {"actor": AGENT},
        {"created_by": AGENT},
        {"actor": {"name": AGENT}},
        {"actor": AGENT, "reason": "mine now"},
    ],
)
def test_a_smuggled_actor_is_ignored_not_honored(client, fresh_db, smuggled):
    """Property 2 (adversarial smuggling): the field is inert, not an error."""
    proposal = service.create_node(type="note", title="Bot draft", actor=AGENT)
    body = {**smuggled, "ids": [proposal.id]}

    payload = _ok(client.post("/api/review/accept", json=body))

    assert payload["actor"] == service.ACTOR_HUMAN
    assert payload["transitioned"] == [proposal.id]
    accepts = _events("node.accept")
    assert [row.actor for row in accepts] == [service.ACTOR_HUMAN]


def test_a_smuggled_actor_on_a_create_is_ignored(client, fresh_db):
    payload = _ok(
        client.post(
            "/api/nodes?actor=agent:query",
            json={"type": "note", "title": "Smuggled", "actor": AGENT, "created_by": AGENT},
            headers={"X-Actor": AGENT},
        )
    )
    assert payload["created_by"] == service.ACTOR_HUMAN
    # Landing `active` *is* the human attribution: an agent write lands proposed.
    assert payload["state"] == "active"


def test_an_agent_proposal_accepted_over_http_is_a_human_write(client, fresh_db):
    """Property 3 (end-to-end): propose as an agent, accept over HTTP."""
    target = service.create_node(type="concept", title="Osmosis")
    proposal = service.create_node(
        type="note", title="Agent draft", content="see [[Osmosis]]", actor=AGENT
    )
    pending_edge = service.list_edges(node_id=proposal.id)[0]
    assert pending_edge.state == "proposed"

    queue = _ok(client.get("/api/review/queue"))
    assert {row["created_by"] for row in queue["proposals"]} == {AGENT}

    _ok(client.post("/api/review/accept", json={"ids": [proposal.id]}))

    node = _ok(client.get(f"/api/nodes/{proposal.id}"))
    assert node["state"] == "active"
    # Authorship survives the review; attribution of the *live-state write*
    # does not transfer with it.
    assert node["created_by"] == AGENT

    accept = _events("node.accept")[0]
    assert accept.actor == service.ACTOR_HUMAN
    assert accept.payload["after"]["state"] == "active"
    # The wikilink edge the agent staged went live under the reviewer's name.
    edge_accept = _events("edge.accept")[0]
    assert edge_accept.actor == service.ACTOR_HUMAN
    assert service.list_edges(node_id=target.id)[0].state == "active"


def test_no_event_that_writes_live_state_is_attributable_to_an_agent(client, fresh_db):
    """Property 4 (log invariant), after exercising every write endpoint.

    The invariant is stated over the log itself rather than over the endpoints:
    any event whose payload puts a row into ``active`` must carry the human
    actor. Agent proposals are in the same log throughout — they simply never
    reach ``active`` on their own.
    """
    concept = _ok(client.post("/api/nodes", json={"type": "concept", "title": "Cells"}))
    note = _ok(
        client.post("/api/nodes", json={"type": "note", "title": "Note", "content": "on [[Cells]]"})
    )
    _ok(client.patch(f"/api/nodes/{note['id']}", json={"content": "revised, still [[Cells]]"}))
    _ok(
        client.post(
            "/api/edges",
            json={"src_id": note["id"], "dst_id": concept["id"], "type": "relates_to"},
        )
    )
    _ok(client.put("/api/policies/agent:bot", json={"rules": []}))
    _ok(client.post("/api/assets", files={"file": ("photo.png", _png_bytes(), "image/png")}))

    accepted = service.create_node(type="note", title="Accept me", actor=AGENT)
    rejected = service.create_node(type="note", title="Reject me", actor=AGENT)
    _ok(client.post("/api/review/accept", json={"ids": [accepted.id]}))
    _ok(client.post("/api/review/reject", json={"ids": [rejected.id], "reason": "off topic"}))

    # Archive, then undo it: the restored row goes back to `active`, which is
    # the write undo makes and the reason it is human-only.
    _ok(client.post(f"/api/nodes/{concept['id']}/archive"))
    _ok(client.post("/api/undo", json={}))
    assert service.get_node(concept["id"]).state == "active"

    live_writes = 0
    for event in service.list_events(limit=500):
        for row in (event.payload.get("after"), event.payload.get("restored")):
            if isinstance(row, dict) and row.get("state") == "active":
                live_writes += 1
                assert event.actor == service.ACTOR_HUMAN, f"{event.op} at seq {event.seq}"
    # A vacuous pass would be worthless: the run really did write live state.
    assert live_writes >= 6
    assert any(event.actor == AGENT for event in service.list_events(limit=500))


def test_a_reject_without_a_reason_is_refused(client, fresh_db):
    """The CLI's audit guarantee, mirrored: no reason, no reject."""
    proposal = service.create_node(type="note", title="Bot draft", actor=AGENT)
    for body in ({"ids": [proposal.id]}, {"ids": [proposal.id], "reason": "   "}):
        response = client.post("/api/review/reject", json=body)
        assert response.status_code == 400
        assert "reason" in response.json()["error"]["message"]
    assert service.get_node(proposal.id).state == "proposed"


def test_a_bodyless_review_refuses_to_touch_the_whole_queue(client, fresh_db):
    service.create_node(type="note", title="Bot draft", actor=AGENT)
    response = client.post("/api/review/accept", json={})
    assert response.status_code == 400
    assert "whole queue" in response.json()["error"]["message"]
    assert len(service.list_proposals()) == 1


# ── 4. Auth ───────────────────────────────────────────────────────────────────


def test_a_configured_token_gates_the_api_and_nothing_else(fresh_db):
    app = http_api.create_app(token="s3cret")
    anonymous, wrong, right = Client(app), Client(app, token="nope"), Client(app, token="s3cret")

    refused = anonymous.get("/api/types")
    assert refused.status_code == 401
    assert refused.json()["error"]["type"] == "Unauthorized"
    assert wrong.get("/api/types").status_code == 401
    assert right.get("/api/types").status_code == 200

    # A liveness probe that needs credentials is not a liveness probe, and the
    # page that holds the token cannot itself require it.
    assert anonymous.get("/healthz").status_code == 200
    assert anonymous.get("/").status_code == 200


def test_no_token_means_no_auth_at_all(client, fresh_db):
    assert client.get("/api/types").status_code == 200
    assert client.get("/healthz").status_code == 200


def test_healthz_reports_the_database_it_serves(client, fresh_db):
    payload = _ok(client.get("/healthz"))
    assert payload["status"] == "ok"
    assert payload["db_path"].endswith("graph.db")


# ── 5. Static hosting ─────────────────────────────────────────────────────────


def test_the_built_bundle_is_served_and_client_routes_fall_through(client, bundle):
    assert "nodum SPA" in client.get("/").text
    assert "console.log" in client.get("/assets/app.js").text
    # Deep links are the browser router's, so they resolve to the shell.
    assert "nodum SPA" in client.get("/graph/abc123").text
    assert "nodum SPA" in client.get("/editor/abc123").text
    assert client.get("/api/types").status_code == 200


def test_a_missing_bundle_serves_the_placeholder_without_crashing(client, monkeypatch, tmp_path):
    monkeypatch.setattr(http_api, "WEB_ROOT", tmp_path / "never-built")

    for path in ("/", "/editor/xyz"):
        response = client.get(path)
        assert response.status_code == 200
        assert "UI not built" in response.text
        assert "make web-build" in response.text

    # The API and the probe are unaffected by an unbuilt UI.
    assert client.get("/api/types").status_code == 200
    assert client.get("/healthz").status_code == 200


def test_an_empty_bundle_directory_also_falls_back(client, tmp_path, monkeypatch):
    empty = tmp_path / "_web"
    empty.mkdir()
    monkeypatch.setattr(http_api, "WEB_ROOT", empty)
    assert "UI not built" in client.get("/").text


def test_favicon_is_answered_as_an_icon_not_as_the_spa_shell(client, bundle):
    """A browser asks for /favicon.ico on its own; the catch-all must not own it.

    Returning ``index.html`` under a 200 and ``text/html`` is a lie the client
    cannot detect — it asked for an image and was told it got one.
    """
    response = client.get("/favicon.ico")

    assert response.status_code == 204
    assert response.text == ""
    assert "text/html" not in response.headers.get("content-type", "")
    assert "nodum SPA" not in response.text

    # The catch-all is still the catch-all for everything that *is* a client
    # route — the point is one exemption, not a new gap.
    assert "nodum SPA" in client.get("/graph/abc123").text
    assert "nodum SPA" in client.get("/editor/abc123").text
    assert "nodum SPA" in client.get("/history/abc123").text
    assert "nodum SPA" in client.get("/favicon.ico.html").text


def test_a_real_favicon_in_the_bundle_is_served(client, bundle):
    """Dropping an icon into the bundle needs no change to the route."""
    (bundle / "favicon.ico").write_bytes(b"\x00\x00\x01\x00")

    response = client.get("/favicon.ico")

    assert response.status_code == 200
    assert response.content == b"\x00\x00\x01\x00"


def test_favicon_is_still_not_html_without_a_bundle(client, monkeypatch, tmp_path):
    """The unbuilt-UI fallback must not answer an icon request with the placeholder."""
    monkeypatch.setattr(http_api, "WEB_ROOT", tmp_path / "never-built")

    response = client.get("/favicon.ico")

    assert response.status_code == 204
    assert "UI not built" not in response.text


def test_static_paths_never_escape_the_bundle(client, bundle, tmp_path):
    (tmp_path / "secret.txt").write_text("do not serve me", encoding="utf-8")

    assert http_api._web_file("../secret.txt") is None
    assert http_api._web_file("assets/app.js") is not None
    assert "do not serve me" not in client.get("/%2e%2e/secret.txt").text


# ── 6. The endpoint surface ───────────────────────────────────────────────────


def test_types_and_schema(client, fresh_db):
    catalog = _ok(client.get("/api/types"))
    assert {row["id"] for row in catalog["node_types"]} >= {"note", "concept"}
    assert _ok(client.get("/api/schema/note"))["name"] == "note"
    assert _ok(client.get("/api/schema/mentions"))["name"] == "mentions"


def test_node_lifecycle_over_http(client, fresh_db):
    created = _ok(client.post("/api/nodes", json={"type": "note", "title": "Draft"}))
    assert created["state"] == "active"

    child = _ok(
        client.post(
            "/api/nodes", json={"type": "note", "title": "Child", "parent_id": created["id"]}
        )
    )
    assert _ok(client.get(f"/api/nodes/{created['id']}/children"))["nodes"][0]["id"] == child["id"]

    updated = _ok(
        client.patch(f"/api/nodes/{created['id']}", json={"title": "Kept", "props": {"k": 1}})
    )
    assert (updated["title"], updated["props"]) == ("Kept", {"k": 1})
    assert client.patch(f"/api/nodes/{created['id']}", json={}).status_code == 400

    history = _ok(client.get(f"/api/nodes/{created['id']}/history"))
    assert history["count"] == 2
    diff = _ok(
        client.get(f"/api/diff?a={history['versions'][0]['id']}&b={history['versions'][1]['id']}")
    )
    assert "title" in diff["changed_fields"]

    assert _ok(client.post(f"/api/nodes/{child['id']}/archive"))["state"] == "archived"


def test_node_get_with_depth_returns_the_neighborhood(client, fresh_db):
    hub = service.create_node(type="concept", title="Hub")
    service.create_node(type="note", title="Leaf", content="[[Hub]]")

    bare = _ok(client.get(f"/api/nodes/{hub.id}"))
    assert "nodes" not in bare

    neighborhood = _ok(client.get(f"/api/nodes/{hub.id}?depth=1"))
    assert neighborhood["root"] == hub.id
    assert len(neighborhood["nodes"]) == 2
    assert neighborhood["truncated"] is False
    assert client.get(f"/api/nodes/{hub.id}?depth=-1").status_code == 400
    assert client.get(f"/api/nodes/{hub.id}?depth=abc").status_code == 400


def test_edges_list_and_create(client, fresh_db):
    a = service.create_node(type="concept", title="A")
    b = service.create_node(type="concept", title="B")
    edge = _ok(
        client.post(
            "/api/edges",
            json={"src_id": a.id, "dst_id": b.id, "type": "relates_to", "confidence": 0.5},
        )
    )
    assert (edge["state"], edge["created_by"]) == ("active", service.ACTOR_HUMAN)

    listing = _ok(client.get(f"/api/edges?node_id={a.id}&type=relates_to"))
    assert listing["count"] == 1
    assert client.post("/api/edges", json={"src_id": a.id, "dst_id": b.id}).status_code == 400
    bad = client.post(
        "/api/edges", json={"src_id": a.id, "dst_id": b.id, "type": "relates_to", "confidence": 5}
    )
    assert bad.status_code == 400


def test_search_and_link_suggestions(client, fresh_db):
    service.create_node(type="note", title="Osmosis in plants", content="water moves")

    hits = _ok(client.get("/api/search?q=osmosis&limit=5"))
    assert hits["k"] == 5
    assert hits["hits"][0]["title"] == "Osmosis in plants"
    assert "bm25" in hits["hits"][0]["signals"]
    assert _ok(client.get("/api/search?q=osmosis&state=any&expand=true"))["hits"]
    assert client.get("/api/search").status_code == 400
    assert client.get("/api/search?q=osmosis&expand=maybe").status_code == 400

    suggestions = _ok(client.get("/api/links/suggest?prefix=Osm&limit=5"))
    assert suggestions["nodes"][0]["title"] == "Osmosis in plants"
    assert _ok(client.get("/api/links/suggest?prefix="))["count"] == 1
    assert client.get("/api/links/suggest?prefix=a&limit=0").status_code == 400


def test_graph_subgraph_and_path(client, fresh_db):
    hub = service.create_node(type="concept", title="Hub")
    leaves = [
        service.create_node(type="note", title=f"Leaf {i}", content="[[Hub]]") for i in range(3)
    ]

    capped = _ok(client.get(f"/api/graph/subgraph?root={hub.id}&limit=2"))
    assert len(capped["nodes"]) == 2
    assert capped["truncated"] is True

    filtered = _ok(
        client.get(
            f"/api/graph/subgraph?root={hub.id}&edge_type=mentions&edge_type=relates_to"
            "&node_type=note&edge_state=active&depth=2&limit=50"
        )
    )
    assert {row["id"] for row in filtered["nodes"]} == {hub.id, *(leaf.id for leaf in leaves)}
    assert client.get("/api/graph/subgraph").status_code == 400
    assert client.get(f"/api/graph/subgraph?root={hub.id}&limit=0").status_code == 400

    path = _ok(client.get(f"/api/graph/path?a={leaves[0].id}&b={hub.id}"))
    assert path["found"] is True and path["hops"] == 1


def test_review_queue_filters_and_batch_forms(client, fresh_db):
    first = service.create_node(type="note", title="One", actor=AGENT)
    second = service.create_node(type="note", title="Two", actor="agent:other")

    assert _ok(client.get("/api/review/queue"))["count"] == 2
    assert _ok(client.get(f"/api/review/queue?agent={AGENT}"))["count"] == 1
    assert _ok(client.get("/api/review/queue?kind=node&limit=1"))["count"] == 1

    accepted = _ok(client.post("/api/review/accept", json={"created_by": AGENT}))
    assert accepted["transitioned"] == [first.id]
    rejected = _ok(client.post("/api/review/reject", json={"ids": [second.id], "reason": "no"}))
    assert rejected["reason"] == "no"
    assert service.get_node(second.id).state == "archived"
    reject_event = _events("node.reject")[0]
    assert reject_event.payload["reason"] == "no"


def test_policies_read_and_write(client, fresh_db):
    rules = [{"edge_type": "mentions", "action": "auto_accept"}]
    stored = _ok(client.put("/api/policies/agent:bot", json={"rules": rules}))
    assert stored["rules"] == rules
    assert stored["updated_by"] == service.ACTOR_HUMAN

    assert _ok(client.get("/api/policies"))["count"] == 1
    assert _ok(client.get("/api/policies/agent:bot"))["agent"] == "agent:bot"

    assert client.put("/api/policies/agent:bot", json={}).status_code == 400
    malformed = client.put("/api/policies/agent:bot", json={"rules": [{"action": "nope"}]})
    assert malformed.status_code == 400


def test_disabling_a_policy_is_an_explicit_empty_ruleset(client, fresh_db):
    """No adapter-invented ``enabled`` flag: the body is ``{"rules": [...]}`` only.

    ``PolicyOut`` has no ``enabled`` field and the service's only
    representation of "disabled" is an empty ruleset, so a caller that wants to
    disable must say so and see it. A flag would have wiped a stored ruleset on
    a value the domain cannot express — silently destructive, and unrecoverable
    once the response shows ``rules: []``.
    """
    rules = [{"edge_type": "mentions", "action": "auto_accept"}]
    _ok(client.put("/api/policies/agent:bot", json={"rules": rules}))

    disabled = _ok(client.put("/api/policies/agent:bot", json={"rules": []}))
    assert disabled["rules"] == []
    assert service.get_policy("agent:bot").rules == []

    # An `enabled` key is inert: it is not read, so the rules still land.
    untouched = _ok(client.put("/api/policies/agent:bot", json={"rules": rules, "enabled": False}))
    assert untouched["rules"] == rules
    assert service.get_policy("agent:bot").rules == rules
    assert "enabled" not in _ok(client.get("/api/policies/agent:bot"))


def test_asset_upload_list_get_and_rendition(client, fresh_db):
    payload = _png_bytes()
    uploaded = _ok(client.post("/api/assets", files={"file": ("photo.png", payload, "image/png")}))
    assert uploaded["mime"] == "image/png"
    assert uploaded["size_bytes"] == len(payload)
    assert uploaded["original_name"] == "photo.png"

    # Content addressing makes the upload idempotent — same bytes, same row.
    again = _ok(client.post("/api/assets", files={"file": ("copy.png", payload, "image/png")}))
    assert again["hash"] == uploaded["hash"]
    assert _ok(client.get("/api/assets"))["count"] == 1
    assert _ok(client.get(f"/api/assets/{uploaded['hash']}"))["hash"] == uploaded["hash"]

    rendition = client.get(f"/api/assets/{uploaded['hash']}/rendition/thumb")
    assert rendition.status_code == 200
    assert rendition.headers["content-type"] == assets.RENDITION_MIME
    assert Image.open(io.BytesIO(rendition.content)).format == "WEBP"
    # Originals are never served: only the two rendition profiles exist.
    assert client.get(f"/api/assets/{uploaded['hash']}/rendition/full").status_code == 400
    assert client.post("/api/assets", data={"nope": "1"}).status_code == 400


def test_events_and_undo(client, fresh_db):
    node = _ok(client.post("/api/nodes", json={"type": "note", "title": "Undo me"}))

    events = _ok(client.get("/api/events?limit=5"))
    assert events["events"][0]["op"] == "node.create"
    assert events["events"][0]["actor"] == service.ACTOR_HUMAN

    result = _ok(client.post("/api/undo", json={}))
    assert result["undone_op"] == "node.create"
    assert client.get(f"/api/nodes/{node['id']}").status_code == 404
    assert client.post("/api/undo", json={"seq": "nope"}).status_code == 400


def test_export_downloads_the_node_as_json(client, fresh_db):
    hub = service.create_node(type="concept", title="Hub")
    service.create_node(type="note", title="Leaf", content="[[Hub]]")

    response = client.get(f"/api/export/node/{hub.id}?depth=1")
    assert response.status_code == 200
    assert response.headers["content-disposition"] == f'attachment; filename="nodum-{hub.id}.json"'
    payload = json.loads(response.text)
    assert payload["root"] == hub.id
    assert len(payload["nodes"]) == 2


def test_the_cli_serve_command_wires_the_app(fresh_db, monkeypatch):
    """`nodum serve` builds the app and hands it to uvicorn on port 8420."""
    captured: dict = {}

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr("uvicorn.run", fake_run)
    result = runner.invoke(cli.app, ["serve"])

    assert result.exit_code == 0, result.output
    assert (captured["host"], captured["port"]) == ("127.0.0.1", 8420)
    assert Client(captured["app"]).get("/healthz").status_code == 200
