"""HTTP API tests: the ASGI app driven in-process, no socket ever bound.

Mirrors the MCP session pattern — one ``asyncio.run`` per interaction over an
``httpx.ASGITransport``, so the tests themselves stay synchronous and reach the
exact handlers a browser would.

Two properties carry the file.

**The human-only guarantee.** ``test_no_endpoint_can_attribute_a_write_to_an
_agent`` drives *every route in the live route table* with actor-carrying
bodies and then asserts that nothing written during the sweep names anything
but ``human``. It knows no endpoint by name, tests no mechanism, and needs no
list to maintain — a rogue handler added tomorrow is swept because it is in
``app.routes``. The AST properties beside it are the belt to that braces: one
``actor=`` binding, no import of an actor-taking service function under any
name or alias, no ``getattr`` on an adapter module, no unreviewed ``**`` unpack.
The earlier versions of those AST tests were all evadable — a handler doing
``service_create_node(**body, path=db_path)`` passed every one of them — which
is why the runtime sweep, not the AST, is now the load-bearing test.

**Origin control.** ``nodum serve`` binds loopback with no token, and loopback
is reachable from every page the browser loads. The tests in §4 replay the
cross-origin form post, the ``Host``-rebinding read, and the oversized upload
that a reviewer landed against the unguarded app.
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
from starlette.requests import ClientDisconnect
from typer.testing import CliRunner

from nodum import assets, cli, http_api, service

AGENT = "agent:researcher"
#: The tests are a non-browser client on the loopback interface, which is what
#: the ``Host`` allowlist answers to.
BASE_URL = "http://127.0.0.1:8420"
#: What a curl-shaped client sends to say it is not a browser (see
#: ``RequestGuardMiddleware``). The ``Client`` below sends it on every write, so
#: individual tests read as they did before origin control existed.
CLIENT_HEADERS = {http_api.CLIENT_HEADER: "nodum-tests"}
#: A browser on another origin, as the headers it cannot avoid sending.
CROSS_ORIGIN_HEADERS = {
    "Origin": "https://evil.example",
    "Sec-Fetch-Site": "cross-site",
    "Sec-Fetch-Mode": "no-cors",
}

runner = CliRunner()


class Client:
    """Synchronous in-process driver over the ASGI app.

    ``raise_app_exceptions=False`` keeps an unhandled exception inside the
    response cycle — Starlette's ``ServerErrorMiddleware`` re-raises after
    sending the 500 body so a real server can log the traceback, and the test
    wants to inspect that body.

    It also stands in for a well-behaved non-browser client: it declares itself
    with :data:`CLIENT_HEADERS` and sends ``Content-Type: application/json`` on
    a bodyless write, exactly as ``web/src/api/client.ts`` now does. Tests that
    want the *unguarded* request pass ``guard=False``.
    """

    def __init__(self, app, token: str | None = None) -> None:
        self.app = app
        # Bytes, not str: httpx encodes a str header as ASCII, and a token is
        # allowed to hold anything a byte string can.
        self.headers = {"Authorization": f"Bearer {token}".encode()} if token else {}

    def request(self, method: str, path: str, guard: bool = True, **kwargs) -> httpx.Response:
        """Issue one request and return the response."""
        headers = {**self.headers}
        if guard and method not in ("GET", "HEAD", "OPTIONS"):
            headers.update(CLIENT_HEADERS)
            # A bodyless write still has to declare JSON: that is the whole
            # point of requiring the content type on every write, not just the
            # ones that carry a body.
            if not kwargs.keys() & {"json", "data", "files", "content"}:
                headers["Content-Type"] = "application/json"
        headers.update(kwargs.pop("headers", {}))

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

    def delete(self, path: str, **kwargs) -> httpx.Response:
        return self.request("DELETE", path, **kwargs)


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
    ("path", "status", "error_type"),
    [
        # A Python bignum: parsed fine, then failed inside the sqlite3 driver
        # as an OverflowError and came back a 500 on all four endpoints.
        ("/api/nodes?limit=99999999999999999999999", 400, "ValueError"),
        ("/api/edges?limit=99999999999999999999999", 400, "ValueError"),
        ("/api/events?limit=99999999999999999999999", 400, "ValueError"),
        ("/api/search?q=x&limit=99999999999999999999999", 400, "ValueError"),
        # Same class of bug one parameter over: inf and nan are floats SQLite
        # will happily store and silently mis-compare.
        ("/api/graph/subgraph?root=x&min_confidence=inf", 400, "ValueError"),
        ("/api/graph/subgraph?root=x&min_confidence=nan", 400, "ValueError"),
    ],
)
def test_a_hostile_parameter_is_a_400_from_the_real_endpoint(
    client, fresh_db, path, status, error_type
):
    """Real provocations through real endpoints, not a monkeypatched stand-in.

    The test this replaced was parametrised over ``EXCEPTION_STATUS`` itself and
    monkeypatched ``service.list_types`` to raise each key in turn. It could
    only ever confirm that what is in the table is in the table — it could not
    detect a *missing* mapping, and every one of the 500s below was invisible
    to it.
    """
    response = client.get(path)
    assert response.status_code == status, response.text
    assert response.json()["error"]["type"] == error_type
    assert "Traceback" not in response.text


def test_a_database_that_is_not_a_database_is_not_a_500(tmp_path, fresh_db):
    """``sqlite3.DatabaseError`` was unmapped; the CLI prints it and exits 1."""
    not_a_db = tmp_path / "notes.txt"
    not_a_db.write_text("this is not a SQLite file", encoding="utf-8")
    client = Client(http_api.create_app(db_path=not_a_db))

    response = client.get("/api/nodes")

    assert response.status_code == 500
    body = response.json()["error"]
    assert body["type"] == "DatabaseError"
    assert body["message"].startswith("database error: ")
    assert "not a database" in body["message"]


def test_a_locked_database_is_still_the_retryable_503(client, fresh_db, monkeypatch):
    """The one sqlite3 subclass that is retryable keeps its own status."""

    def raise_locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(service, "list_types", raise_locked)
    response = client.get("/api/types")

    assert response.status_code == 503
    assert response.json()["error"]["message"] == "database error: database is locked"


def test_an_os_error_is_mapped_and_does_not_echo_the_path(client, fresh_db, monkeypatch):
    """``cli._run`` catches OSError; so must this table — but without the path.

    The CLI appends the filename because it is the operator's own. Over a
    socket it is a stranger's, and a username plus a filesystem layout is not
    something a failed read should hand out.
    """

    def raise_oserror(*args, **kwargs):
        raise PermissionError(13, "Permission denied", "/home/alice/.local/share/nodum/nodum.db")

    monkeypatch.setattr(service, "list_types", raise_oserror)
    response = client.get("/api/types")

    assert response.status_code == 500
    assert response.json()["error"]["type"] == "PermissionError"
    assert "/home/alice" not in response.text
    assert "Permission denied" in response.text


def test_every_exception_cli_run_catches_is_mapped():
    """The docstring's claim, asserted rather than repeated.

    ``AGENTS.md`` and the module docstring both said the table held "exactly the
    ones ``cli._run`` catches". It did not: ``cli._run`` catches ``sqlite3.Error``
    and ``OSError``, and the table listed only ``sqlite3.OperationalError``, so
    ``DatabaseError``, ``IntegrityError``, ``ProgrammingError``, ``DataError``
    and every ``OSError`` were unmapped 500s.
    """
    caught = _cli_run_caught_exceptions()
    assert caught, "could not read the exception list out of cli._run"
    for exception in caught:
        assert any(issubclass(exception, mapped) for mapped in http_api.EXCEPTION_STATUS), (
            f"{exception.__name__} is caught by cli._run but unmapped in EXCEPTION_STATUS"
        )

    # And the classes only a network surface meets, which the CLI cannot.
    assert http_api.EXCEPTION_STATUS[http_api.PayloadTooLarge] == 413
    assert http_api.EXCEPTION_STATUS[ClientDisconnect] == 499


def _cli_run_caught_exceptions() -> list[type[BaseException]]:
    """Resolve the exception classes named in ``cli._run``'s except clauses."""
    tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
    run = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_run"
    )
    names: list[str] = []
    for handler in (node for node in ast.walk(run) if isinstance(node, ast.ExceptHandler)):
        for node in ast.walk(handler.type) if handler.type else ():
            if isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.Attribute):
                names.append(f"{getattr(node.value, 'id', '')}.{node.attr}")
    resolved: list[type[BaseException]] = []
    for name in names:
        module, _, attribute = name.rpartition(".")
        owner = getattr(cli, module) if module else cli
        candidate = getattr(owner, attribute, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            resolved.append(candidate)
    return resolved


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


def _actor_taking_service_functions() -> set[str]:
    """Service functions that accept an ``actor`` — i.e. every write."""
    return {
        name
        for name, function in vars(service).items()
        if inspect.isfunction(function)
        and function.__module__ == service.__name__
        and "actor" in inspect.signature(function).parameters
    }


def _swept_requests(app, ids: dict[str, str]) -> list[tuple[str, str, int]]:
    """Fire an actor-carrying request at every method of every route in ``app``.

    The route table is the input, so a handler added later is swept without
    anyone remembering it exists. Several body shapes are tried because a rogue
    handler's signature is unknown: a bare ``{"actor": …}`` reaches one that
    forwards a whole body, and the fuller shapes reach one that also needs a
    type or a title before it will do any work.
    """
    client = Client(app)
    bodies = [
        {"actor": AGENT},
        {"type": "note", "title": "swept", "actor": AGENT},
        {"type": "note", "title": "swept", "created_by": AGENT},
        {
            "type": "note",
            "title": "swept",
            "content": "swept",
            "src_id": ids["node"],
            "dst_id": ids["other"],
            "ids": [ids["proposal"]],
            "reason": "swept",
            "rules": [],
            "actor": AGENT,
            "created_by": AGENT,
            "updated_by": AGENT,
        },
    ]
    fired: list[tuple[str, str, int]] = []
    for route in app.routes:
        path = route.path
        for name, value in ids.items():
            path = path.replace(f"{{{name}}}", value)
        path = (
            path.replace("{id}", ids["node"])
            .replace("{type}", "note")
            .replace("{agent}", AGENT)
            .replace("{profile}", "thumb")
            .replace("{path:path}", "swept")
        )
        for method in sorted(route.methods or set()):
            if method in ("GET", "HEAD", "OPTIONS"):
                continue
            for body in bodies:
                response = client.request(
                    method, f"{path}?actor={AGENT}", json=body, headers={"X-Actor": AGENT}
                )
                fired.append((method, path, response.status_code))
    return fired


def test_no_endpoint_can_attribute_a_write_to_an_agent(fresh_db):
    """The human-only guarantee, as one property over the whole route table.

    This is the load-bearing test, and it knows nothing about how the boundary
    is implemented — not ``_write``, not ``HTTP_ACTOR``, not which endpoints
    exist. It drives every state-changing method of every route with bodies,
    query strings and headers that all claim an agent identity, and then asks
    the database one question: did anything written during that sweep end up
    attributed to something other than the human?

    Every AST test in this file was evadable. A handler as short as::

        from nodum.service import create_node as _service_create_node
        async def quick_create(request):
            return EnvelopeResponse(envelope(_service_create_node(**body, path=db_path)))

    named no actor (so the source scan passed), bound no ``actor=`` keyword
    (a ``**`` unpack has ``arg=None``, so the binding count passed), and called
    no ``service.<name>`` attribute (so the direct-call scan passed) — while
    ``POST`` with ``{"actor": "agent:evil"}`` produced ``created_by:
    "agent:evil"``. This test fails on it, because the row it writes is in the
    same database the assertion reads.
    """
    app = http_api.create_app()
    node = service.create_node(type="concept", title="Sweep target")
    other = service.create_node(type="concept", title="Sweep other")
    proposal = service.create_node(type="note", title="Sweep proposal", actor=AGENT)
    ids = {"node": node.id, "other": other.id, "proposal": proposal.id}

    before_seq = max((event.seq for event in service.list_events(limit=5000)), default=0)
    before_nodes = {row.id for row in service.list_nodes(limit=5000)}
    before_edges = {row.id for row in service.list_edges(limit=5000)}

    fired = _swept_requests(app, ids)

    # A sweep that never reached a handler would pass vacuously.
    assert len(fired) >= 40, fired
    assert sum(1 for _, _, status in fired if status < 300) >= 5, fired

    new_events = [event for event in service.list_events(limit=5000) if event.seq > before_seq]
    assert new_events, "the sweep wrote nothing, so it proves nothing"
    offenders = [
        (event.op, event.seq, event.actor)
        for event in new_events
        if event.actor != service.ACTOR_HUMAN
    ]
    assert offenders == [], f"writes attributed to a non-human actor: {offenders}"

    written_nodes = [row for row in service.list_nodes(limit=5000) if row.id not in before_nodes]
    written_edges = [row for row in service.list_edges(limit=5000) if row.id not in before_edges]
    assert {row.created_by for row in written_nodes} <= {service.ACTOR_HUMAN}
    assert {row.created_by for row in written_edges} <= {service.ACTOR_HUMAN}


def test_the_route_table_holds_only_endpoints_the_sweep_can_reach(fresh_db):
    """Nothing may hide behind a raw ASGI app or a mount.

    ``_route_endpoints`` reads ``route.endpoint``, which is ``None`` for a
    ``Mount("/x", app=<raw ASGI app>)`` — so a mount would be invisible to every
    source-level property in this file, and its own routing would be invisible
    to the sweep above. There are none; this is the test that keeps it that way.
    """
    app = http_api.create_app()
    endpoints = _route_endpoints(app)
    assert len(endpoints) >= 30
    assert {path for path, _ in endpoints} >= {"/healthz", "/api/nodes", "/{path:path}"}
    assert len(endpoints) == len(app.routes), "every route must expose a Python endpoint"
    assert all(
        getattr(route, "app", None) is None or hasattr(route, "endpoint") for route in app.routes
    )


def test_the_installed_middleware_is_exactly_what_this_file_reviewed(fresh_db):
    """A write performed in middleware is walked by no AST test in this file.

    The sweep would catch one that writes on a swept request, but middleware can
    also act on a *read*. Pinning the list means a new layer cannot arrive
    unreviewed: adding one is a test failure until someone writes down why.
    """
    plain = [entry.cls for entry in http_api.create_app().user_middleware]
    with_token = [entry.cls for entry in http_api.create_app(token="x").user_middleware]

    assert plain == [http_api.RequestGuardMiddleware]
    assert with_token == [http_api.RequestGuardMiddleware, http_api.BearerTokenMiddleware]


def test_no_route_handler_can_read_an_actor_from_a_request(fresh_db):
    """Enumerated absence over the live route table: no handler names an actor.

    Cheap, and it catches the obvious version of the mistake — a handler that
    reads ``request.query_params["actor"]``. It does **not** catch a handler
    that forwards a body it never inspects, which is why it is no longer the
    test the guarantee rests on.
    """
    endpoints = _route_endpoints(http_api.create_app())
    offenders = [
        path for path, endpoint in endpoints if "actor" in inspect.getsource(endpoint).casefold()
    ]
    assert offenders == [], f"route handlers must never name an actor: {offenders}"


def test_the_actor_is_bound_exactly_once_and_to_the_human_constant():
    """One binding site, and it is a constant."""
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


#: Values that may be splatted into a call in ``nodum.http_api``. Each one is
#: allowlisted at source, so what it produces cannot contain an identity:
#: ``_proposal_filters``/``_selective_filters`` read only ``PROPOSAL_FILTERS``,
#: ``fields`` is the ``PATCHABLE_FIELDS`` comprehension in ``update_node``, and
#: ``kwargs`` is ``_write``'s own forward — the one place that *does* receive a
#: caller's dict wholesale, and the one that refuses an ``actor`` key outright
#: (``test_the_write_helper_refuses_a_caller_supplied_actor``).
ALLOWED_UNPACK_SOURCES = {"_proposal_filters", "_selective_filters", "fields", "kwargs"}


def test_no_call_splats_anything_but_an_allowlisting_helper():
    """``**`` is how request data reaches a service call without being named.

    The binding count above sees only ``ast.keyword(arg="actor")``; a ``**``
    unpack has ``arg=None`` and slid straight past it, which is exactly how
    ``_service_create_node(**body, path=db_path)`` scored a clean run while
    writing ``created_by: "agent:evil"``. Every unpack in the module must
    therefore name a helper that decides the keys itself — and a new one is a
    failure here until a human adds it to :data:`ALLOWED_UNPACK_SOURCES` and
    says why.
    """
    unpacked = [
        keyword.value
        for node in ast.walk(_module_ast())
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg is None
    ]
    assert unpacked, "the module used to contain ** unpacks; if it no longer does, drop this test"
    sources = set()
    for value in unpacked:
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
            sources.add(value.func.id)
        elif isinstance(value, ast.Name):
            sources.add(value.id)
        else:
            sources.add(ast.dump(value))
    assert sources <= ALLOWED_UNPACK_SOURCES, (
        f"unreviewed ** unpack of {sorted(sources - ALLOWED_UNPACK_SOURCES)}: "
        "a service call may only be splatted with a dict a helper allowlisted"
    )


def test_no_write_service_function_is_reachable_under_any_name():
    """A handler cannot reach a write without ``_write`` — by name, alias or getattr.

    The version this replaces matched ``ast.Attribute`` calls whose value was
    literally the name ``service``, so three spellings walked past it: a bare
    name (``from nodum.service import create_node``), an alias
    (``… as _service_create_node`` — the one a reviewer actually used), and
    ``getattr(service, "create_node")``. All three are closed here, and the
    import ban is the one that does the work: an alias renames the *local*
    name, never the name in the ``ImportFrom`` node.
    """
    writers = _actor_taking_service_functions()
    assert writers >= {
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
    tree = _module_ast()
    adapter_modules = {"service", "assets", "search_module", "db"}

    imported = [
        f"{node.module}.{alias.name}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if (node.module or "").startswith("nodum") and alias.name in writers
    ]
    assert imported == [], (
        f"never import a service write into this module — an alias hides it: {imported}"
    )

    called = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr in writers:
            called.append(node.func.attr)
        elif isinstance(node.func, ast.Name) and node.func.id in writers:
            called.append(node.func.id)
    assert called == [], f"these must go through _write(): {sorted(set(called))}"

    reflective = [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("getattr", "__import__")
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id in adapter_modules
    ]
    assert reflective == [], f"no reflective lookup on an adapter module: {reflective}"


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


def test_a_non_ascii_bearer_header_is_a_401_not_a_500(fresh_db):
    """``secrets.compare_digest`` refuses non-ASCII ``str``; headers are latin-1.

    ``Authorization: Bearer café`` raised ``TypeError`` inside the middleware —
    an unauthenticated, endlessly repeatable 500 with a full traceback per
    request, on exactly the deployment that configured a token. Comparing bytes
    makes it the 401 it always was.
    """
    client = Client(http_api.create_app(token="s3cret"))

    # Raw bytes on the wire: a header is not required to be ASCII, which is the
    # whole problem — Starlette decodes it as latin-1 and hands over a str that
    # `compare_digest` refuses.
    for header in (b"Bearer caf\xe9", b"Bearer \xff\xfe", b"Bearer " + b"\xfc" * 64):
        response = client.get("/api/types", headers={"Authorization": header})
        assert response.status_code == 401, header
        assert response.json()["error"]["type"] == "Unauthorized"

    # A correct token that is itself non-ASCII still authenticates.
    utf8 = Client(http_api.create_app(token="clé-café"), token="clé-café")
    assert utf8.get("/api/types").status_code == 200


def test_the_auth_gate_and_the_router_agree_on_what_an_api_path_is(fresh_db):
    """``//api/nodes`` used to be an API path to the gate and a SPA path to the router.

    Nothing leaked — both spellings fell through to the SPA — but a gate and a
    router keyed differently is a bug with one half missing. Paths are
    normalised once, at the outermost layer, so ``//api/nodes`` is now an API
    path to *both* and is gated like one.
    """
    client = Client(http_api.create_app(token="s3cret"))
    # Absolute, because httpx reads a leading `//` in a relative URL as
    # scheme-relative and would rewrite the host out from under the test.
    doubled = f"{BASE_URL}//api/nodes"

    assert client.get(doubled).status_code == 401
    assert client.get(f"{BASE_URL}/api//nodes").status_code == 401
    # A different case is a different path to the router, so it is not an API
    # path to either side — it falls through to the SPA, consistently.
    assert client.get("/API/nodes").status_code == 200
    assert client.get("/api/nodes").status_code == 401

    authorised = Client(http_api.create_app(token="s3cret"), token="s3cret")
    assert authorised.get(doubled).status_code == 200


# ── 4b. Origin control: the browser cannot drive this API ─────────────────────


CSRF_WRITES = [
    ("POST", "/api/review/accept"),
    ("POST", "/api/undo"),
    ("POST", "/api/nodes"),
    ("POST", "/api/edges"),
    ("PATCH", "/api/nodes/x"),
    ("PUT", "/api/policies/agent:bot"),
]


@pytest.mark.parametrize(("method", "path"), CSRF_WRITES)
def test_a_cross_origin_write_is_refused(client, fresh_db, method, path):
    """A page on another origin cannot reach a write, whatever it claims.

    ``Origin`` and ``Sec-Fetch-Site`` are forbidden header names — script cannot
    set or suppress either — so a browser that is cross-site says so, and this
    is where saying so stops mattering to the database.
    """
    response = client.request(
        method,
        path,
        guard=False,
        headers={**CROSS_ORIGIN_HEADERS, "Content-Type": "application/json"},
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["type"] == "CrossOriginRequest"


def test_the_text_plain_form_post_that_accepted_an_agents_proposal(client, fresh_db):
    """The reported exploit, replayed end to end.

    A page anywhere on the web could submit ``<form action="http://127.0.0.1:8420
    /api/review/accept" method="post" enctype="text/plain">`` — a CORS-*simple*
    request, so no preflight, so the absence of CORS headers stopped the
    attacker reading the reply and nothing else. It returned 200, moved an
    agent's proposal to ``active``, and recorded the event as ``actor: human``:
    the log said a human reviewed agent output, and no human had.

    Two independent layers now refuse it: the content type is not
    ``application/json``, and the request is cross-site.
    """
    proposal = service.create_node(type="note", title="Agent draft", actor=AGENT)

    response = client.post(
        "/api/review/accept",
        guard=False,
        headers={**CROSS_ORIGIN_HEADERS, "Content-Type": "text/plain;charset=UTF-8"},
        content=json.dumps({"ids": [proposal.id]}),
    )

    assert response.status_code == 403
    assert service.get_node(proposal.id).state == "proposed"
    assert _events("node.accept") == []


def test_a_bodyless_cross_origin_post_cannot_archive_live_content(client, fresh_db):
    """``fetch(url, {method:'POST', mode:'no-cors'})`` — no body, no content type."""
    node = service.create_node(type="note", title="Live human content")

    response = client.post(
        f"/api/nodes/{node.id}/archive", guard=False, headers=CROSS_ORIGIN_HEADERS
    )

    assert response.status_code == 403
    assert service.get_node(node.id).state == "active"


@pytest.mark.parametrize(
    "content_type",
    ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data", ""],
)
def test_a_json_route_refuses_every_cors_simple_content_type(client, fresh_db, content_type):
    """The structural half: these are the only content types a form can send.

    Requiring ``application/json`` means a cross-origin write needs a preflight,
    and this app answers none — so the request never lands, whatever the origin
    headers say. It applies to bodyless writes too, which is where the check is
    the *only* content-type signal there is.
    """
    headers = {**CLIENT_HEADERS}
    if content_type:
        headers["Content-Type"] = content_type
    response = client.post("/api/nodes", guard=False, headers=headers, content=b"")

    assert response.status_code == 415
    assert response.json()["error"]["type"] == "UnsupportedMediaType"


def test_a_write_with_no_origin_headers_must_say_it_is_not_a_browser(client, fresh_db):
    """curl has to be explicit; a browser never gets here by omission.

    A browser sends ``Origin`` or ``Sec-Fetch-Site`` on every write and cannot
    be talked out of either. A request carrying neither is a non-browser client,
    and it declares itself rather than being waved through — a free pass by
    omission is a free pass something might learn to forge.
    """
    bare = client.post(
        "/api/nodes",
        guard=False,
        headers={"Content-Type": "application/json"},
        json={"type": "note", "title": "curl"},
    )
    assert bare.status_code == 403
    assert http_api.CLIENT_HEADER in bare.json()["error"]["message"]

    declared = client.post("/api/nodes", json={"type": "note", "title": "curl"})
    assert declared.status_code == 200


def test_the_spa_itself_is_not_broken_by_any_of_this(client, fresh_db):
    """Same-origin, as a browser actually sends it: no extra header needed."""
    same_origin = {
        "Origin": BASE_URL,
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Content-Type": "application/json",
    }
    created = client.post(
        "/api/nodes",
        guard=False,
        headers=same_origin,
        json={"type": "note", "title": "From the UI"},
    )
    assert created.status_code == 200
    assert created.json()["created_by"] == service.ACTOR_HUMAN

    # `make web-dev` proxies from :5173 with the browser's own Host and Origin,
    # and ports are not compared, so the dev server keeps working.
    dev = client.post(
        "/api/nodes",
        guard=False,
        headers={
            "Host": "localhost:5173",
            "Origin": "http://localhost:5173",
            "Sec-Fetch-Site": "same-origin",
            "Content-Type": "application/json",
        },
        json={"type": "note", "title": "From the dev proxy"},
    )
    assert dev.status_code == 200


def test_dns_rebinding_is_refused_on_reads_and_writes(client, fresh_db):
    """``Host`` is the only header a rebound page still gets wrong.

    After a rebind the attacker's page *is* same-origin: ``Origin`` matches,
    ``Sec-Fetch-Site`` says ``same-origin``, and a custom header needs no
    preflight. Every check but this one passes, which is why "bind loopback" is
    not a defence against a browser. ``GET /api/nodes`` with
    ``Host: attacker-rebind.example`` used to return 200 and the node content.
    """
    service.create_node(type="note", title="Private content")

    read = client.get("/api/nodes", headers={"Host": "attacker-rebind.example"})
    assert read.status_code == 400
    assert read.json()["error"]["type"] == "UntrustedHost"
    assert "Private content" not in read.text

    write = client.post(
        "/api/nodes",
        guard=False,
        headers={
            "Host": "attacker-rebind.example",
            "Origin": "http://attacker-rebind.example",
            "Sec-Fetch-Site": "same-origin",
            "Content-Type": "application/json",
        },
        json={"type": "note", "title": "rebound"},
    )
    assert write.status_code == 400

    # The static UI and the probe are behind the same check: a rebound page must
    # not be able to load the app either.
    assert client.get("/", headers={"Host": "attacker-rebind.example"}).status_code == 400
    assert client.get("/healthz", headers={"Host": "attacker-rebind.example"}).status_code == 400


def test_an_operator_can_name_the_host_this_server_answers_to(fresh_db):
    """A reverse proxy or a LAN name is configuration, not a hole to leave open."""
    named = Client(
        http_api.create_app(allowed_hosts=http_api.resolve_allowed_hosts("0.0.0.0", ["nodum.lan"]))
    )
    assert named.get("/api/types", headers={"Host": "nodum.lan"}).status_code == 200
    assert named.get("/api/types", headers={"Host": "elsewhere.example"}).status_code == 400

    anywhere = Client(
        http_api.create_app(allowed_hosts=http_api.resolve_allowed_hosts("0.0.0.0", ["*"]))
    )
    assert anywhere.get("/api/types", headers={"Host": "elsewhere.example"}).status_code == 200


# ── 4c. Upload limits ─────────────────────────────────────────────────────────


def test_an_oversized_upload_is_refused_before_it_is_buffered(fresh_db, tmp_path):
    """The cap has to bite in ``receive``, not in ``register_asset``.

    Before this, ``AssetTooLarge`` was the only limit and it fired after
    Starlette had spooled the whole part to disk *and* the handler had copied it
    to a second temp file: a 400 MB upload measured 839 MB of ``/tmp``, and
    tripping the real 1 GB blob limit needed more than 2 GB of it first.
    """
    limit = 64 * 1024
    client = Client(http_api.create_app(max_body_bytes=limit))
    oversized = b"\x00" * (limit * 4)

    declared = client.post("/api/assets", files={"file": ("big.png", oversized, "image/png")})
    assert declared.status_code == 413
    assert declared.json()["error"]["type"] == "PayloadTooLarge"

    # And with no Content-Length at all, so the client-supplied number is not
    # the thing being trusted: a streamed body is chunked, the parser is fed a
    # well-formed part, and the cap has to bite in the middle of it.
    boundary = "nodum-test-boundary"

    async def chunks():
        yield (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="big.png"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode()
        for _ in range(4):
            yield b"\x00" * limit
        yield f"\r\n--{boundary}--\r\n".encode()

    streamed = client.post(
        "/api/assets",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        content=chunks(),
    )
    assert streamed.status_code == 413
    assert assets.list_assets() == []


def test_the_body_cap_covers_json_routes_too(fresh_db):
    """One ceiling on what this server will read, not one per route."""
    client = Client(http_api.create_app(max_body_bytes=4096))
    response = client.post("/api/nodes", json={"type": "note", "content": "x" * 8192})
    assert response.status_code == 413


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("evil.exe", b"MZ\x90\x00this is a program"),
        ("evil.html", b"<html><script>alert(1)</script></html>"),
        ("disk.iso", b"\x00" * 64 + b"CD001"),
        # The interesting one: an executable wearing an image's name. The stored
        # MIME came from `mimetypes.guess_type(name)`, so this used to be
        # registered as `image/png`.
        ("innocent.png", b"MZ\x90\x00this is a program"),
    ],
)
def test_only_real_images_can_be_uploaded(client, fresh_db, name, payload):
    """The type is decided by the bytes, never by the name the client chose."""
    response = client.post("/api/assets", files={"file": (name, payload)})

    assert response.status_code == 400, response.text
    assert response.json()["error"]["type"] == "UnsupportedRendition"
    assert assets.list_assets() == []


def test_a_decompression_bomb_is_refused_at_upload_and_at_rendering(client, fresh_db, tmp_path):
    """Small file, enormous decode — the class, not just the one Pillow shouts at.

    A 612 KB PNG decoding to 14000×14000 raised ``DecompressionBombError`` out
    of the rendition endpoint as a 500. A 375 KB one at 121 MP sat *below*
    Pillow's error threshold, so it simply decoded, at +185 MB of resident
    memory on the event loop. Pixel count refuses both, from the header.
    """
    bomb = tmp_path / "bomb.png"
    Image.new("L", (8000, 8000)).save(bomb, "PNG")

    upload = client.post("/api/assets", files={"file": ("bomb.png", bomb.read_bytes())})
    assert upload.status_code == 400
    assert upload.json()["error"]["type"] == "ImageTooLarge"

    # An asset registered before this check existed (or through the CLI, which
    # still takes any local file) must not be a 500 when a rendition is asked
    # for: the decode is where the memory goes.
    registered = assets.register_asset(bomb, name="bomb.png")
    rendition = client.get(f"/api/assets/{registered.hash}/rendition/thumb")
    assert rendition.status_code == 400
    assert rendition.json()["error"]["type"] == "ImageTooLarge"


# ── 4d. Method handling ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path", "expected_allow"),
    [
        ("DELETE", "/api/nodes", {"GET", "HEAD", "POST"}),
        ("PUT", "/api/nodes/abc", {"GET", "HEAD", "PATCH"}),
        ("GET", "/api/undo", {"POST"}),
        ("POST", "/api/search", {"GET", "HEAD"}),
        ("DELETE", "/api/assets/abc", {"GET", "HEAD"}),
    ],
)
def test_a_wrong_verb_on_a_real_route_is_a_405_not_a_404(
    client, fresh_db, method, path, expected_allow
):
    """The catch-all claims every method, so it out-matched the real route's 405.

    "no such API route: /api/nodes" is not true and sends a client hunting for a
    typo that is not there. Asking the real routes what they *would* have
    matched restores the 405, with the ``Allow`` header that makes it useful.
    """
    response = client.request(method, path)

    assert response.status_code == 405, response.text
    assert response.json()["error"]["type"] == "MethodNotAllowed"
    assert set(response.headers["allow"].split(", ")) == expected_allow


def test_an_unknown_api_path_is_still_a_404(client, fresh_db):
    for method in ("GET", "POST", "DELETE"):
        response = client.request(method, "/api/nope/at/all")
        assert response.status_code == 404
        assert response.json()["error"]["type"] == "NotFound"


def test_healthz_reports_liveness_and_not_the_database_path(fresh_db):
    """A probe needs ``status``, not a filesystem tour.

    ``/healthz`` sits outside auth on purpose, so anything it says is said to
    everyone — and it used to say the absolute database path, disclosing a
    username and a layout even on the ``--token`` deployment that set a token
    precisely to disclose nothing. ``nodum serve`` prints the path at startup
    instead, where the operator is the only reader.
    """
    for app in (http_api.create_app(), http_api.create_app(token="s3cret")):
        payload = _ok(Client(app).get("/healthz"))
        assert payload["status"] == "ok"
        assert "version" in payload
        assert "db_path" not in payload
        assert "/" not in json.dumps(payload)


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


def test_the_spa_is_served_under_a_content_security_policy(client, bundle, monkeypatch, tmp_path):
    """The preview renders agent-written content; a CSP is what holds if it slips.

    Two properties are load-bearing rather than cosmetic: scripts are
    same-origin only — ``'unsafe-inline'`` there would make the whole policy
    ceremonial — and the policy is on the placeholder too, so an unbuilt UI is
    not an unprotected one.
    """
    for path in ("/", "/editor/abc123", "/assets/app.js"):
        policy = client.get(path).headers["content-security-policy"]
        assert "script-src 'self'" in policy
        assert "'unsafe-inline'" not in policy.split("script-src")[1].split(";")[0]
        assert "'unsafe-eval'" not in policy
        assert "object-src 'none'" in policy
        assert "base-uri 'none'" in policy
        assert "frame-ancestors 'none'" in policy

    monkeypatch.setattr(http_api, "WEB_ROOT", tmp_path / "never-built")
    placeholder = client.get("/")
    assert "UI not built" in placeholder.text
    assert "script-src 'self'" in placeholder.headers["content-security-policy"]


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
    # Multipart, but no 'file' part: the route's own validation, not the guard's.
    assert client.post("/api/assets", files={"nope": ("x.txt", b"1")}).status_code == 400
    # A urlencoded form is not multipart, so it never reaches the handler.
    assert client.post("/api/assets", data={"nope": "1"}).status_code == 415


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


def test_a_cancelled_upload_is_a_mapped_outcome_not_a_traceback(fresh_db):
    """``ClientDisconnect`` was unmapped, so every cancelled upload logged a traceback.

    Driven straight against the ASGI app: a cancelled upload is a client that
    stops sending, which reaches the application as ``http.disconnect`` on the
    receive channel — something no HTTP client library can be persuaded to
    reproduce faithfully.
    """
    app = http_api.create_app()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/api/assets",
        "raw_path": b"/api/assets",
        "root_path": "",
        "query_string": b"",
        "scheme": "http",
        "headers": [
            (b"host", b"127.0.0.1:8420"),
            (b"content-type", b"multipart/form-data; boundary=x"),
            (http_api.CLIENT_HEADER.encode(), b"nodum-tests"),
        ],
        "client": ("127.0.0.1", 51000),
        "server": ("127.0.0.1", 8420),
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))

    start = next(message for message in sent if message["type"] == "http.response.start")
    assert start["status"] == 499
    assert b"ClientDisconnect" in b"".join(
        message.get("body", b"") for message in sent if message["type"] == "http.response.body"
    )


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


def test_serve_says_out_loud_that_local_processes_can_drive_it(fresh_db, monkeypatch):
    """The local-process exposure is real and unfixable without a token.

    Any process on this machine can satisfy every origin check with three curl
    headers — including an MCP server launched with ``--actor agent:x``, which
    would thereby regain over HTTP the ``accept`` the MCP tool list structurally
    withholds. That cannot be closed by origin control, so it is *stated*.
    """
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: None)

    open_server = runner.invoke(cli.app, ["serve"])
    assert open_server.exit_code == 0
    assert "any process on this machine" in open_server.output
    assert "agents you do not trust" in open_server.output

    with_token = runner.invoke(cli.app, ["serve", "--token", "s3cret"])
    assert with_token.exit_code == 0
    assert "any process on this machine" not in with_token.output
    # The token has to reach the UI somehow, and the fragment is the one place
    # it does not end up in a log.
    assert "#token=s3cret" in with_token.output


def test_serve_refuses_a_public_bind_with_no_token(fresh_db, monkeypatch):
    """``--host 0.0.0.0`` with no token was accepted silently — an open write API."""
    started: list = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: started.append(kwargs))

    refused = runner.invoke(cli.app, ["serve", "--host", "0.0.0.0"])
    assert refused.exit_code == 1
    assert "refusing to bind" in refused.output
    assert started == []

    allowed = runner.invoke(cli.app, ["serve", "--host", "0.0.0.0", "--token", "s3cret"])
    assert allowed.exit_code == 0
    assert started


def test_serve_exits_1_when_the_port_is_in_use(fresh_db, monkeypatch):
    """uvicorn catches the failed bind itself and exits 3; the contract says 1."""

    def fails_to_start(app, **kwargs):
        raise SystemExit(3)

    monkeypatch.setattr("uvicorn.run", fails_to_start)
    result = runner.invoke(cli.app, ["serve", "--port", "8420"])

    assert result.exit_code == 1
    assert "could not serve on 127.0.0.1:8420" in result.output


def test_serve_allows_extra_hosts_on_the_command_line(fresh_db, monkeypatch):
    """``--allow-host`` is how a reverse proxy or a LAN name gets in."""
    captured: dict = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: captured.update(app=app))

    result = runner.invoke(
        cli.app,
        ["serve", "--host", "0.0.0.0", "--token", "x", "--allow-host", "nodum.lan"],
    )

    assert result.exit_code == 0, result.output
    client = Client(captured["app"], token="x")
    assert client.get("/api/types", headers={"Host": "nodum.lan"}).status_code == 200
    assert client.get("/api/types", headers={"Host": "other.example"}).status_code == 400
