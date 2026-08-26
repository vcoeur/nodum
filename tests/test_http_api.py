"""HTTP API tests: the ASGI app driven in-process, no socket ever bound.

Mirrors the MCP session pattern — one ``asyncio.run`` per interaction over an
``httpx.ASGITransport``, so the tests themselves stay synchronous and reach the
exact handlers a browser would.

Two properties carry the file.

**The session-attribution guarantee.** ``test_writes_are_attributed_to_the
_sessions_human_and_nothing_else`` drives *every route in the live route
table* with actor-carrying bodies behind a second human's session and then
asserts that nothing written during the sweep names anything but that human.
It knows no endpoint by name, tests no mechanism, and needs no list to
maintain — a rogue handler added tomorrow is swept because it is in
``app.routes``. The AST properties beside it are the belt to that braces:
every ``principal=`` mints through ``_session_principal(request)``, no
import of a write service function under any name or alias, no ``getattr``
on an adapter module, no unreviewed ``**`` unpack, no trusted-local ``auth``
entry point. The earlier versions of those AST tests were all evadable — a
handler doing ``service_create_node(**body, path=db_path)`` passed every one
of them — which is why the runtime sweep, not the AST, is now the
load-bearing test.

**Origin control.** ``nodum serve`` binds loopback, and loopback is reachable
from every page the browser loads. The tests in §4b replay the cross-origin
form post, the ``Host``-rebinding read, and the oversized upload that a
reviewer landed against the unguarded app.
"""

from __future__ import annotations

import ast
import asyncio
import codecs
import hashlib
import importlib
import inspect
import io
import json
import pkgutil
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.util import find_spec
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from helpers import OWNER_ACTOR, agent, owner
from PIL import Image
from starlette.requests import ClientDisconnect
from typer.testing import CliRunner

import nodum
from nodum import (
    assets,
    auth,
    cli,
    consolidate,
    db,
    embeddings,
    http_api,
    ingest,
    llm,
    mcp_server,
    projectors,
    service,
    settings,
    urls,
)
from nodum.migrations import GARDENER_AGENT_ID
from nodum.principal import Principal

AGENT = "agent:researcher"
#: The in-process gardener, seeded by migration ``0014``. It is the one
#: principal besides the session's human that a request on this surface can
#: cause a write under — ``POST /api/cycles`` asks the runner to run, and the
#: runner's writes are the gardener's by design (decision G4: the gardener
#: acts, the human asks). Nothing a request says can name it, and
#: ``test_the_only_credential_path_is_the_session`` forbids this module's
#: subject from minting it at all.
GARDENER_ACTOR = f"agent:{GARDENER_AGENT_ID}"
#: The login the default client authenticates with (set on the seeded owner).
OWNER_PASSWORD = "correct horse battery"
#: The tests are a non-browser client on the loopback interface, which is what
#: the ``Host`` allowlist answers to.
BASE_URL = "http://127.0.0.1:8600"
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
#: The two capability-URL routes, spelled out here rather than derived, so that
#: a third one cannot join them by accident: they are the only ``/api`` paths
#: besides login that answer without a session, because the single-use token in
#: the path *is* the credential (``http_api._is_capability_path``). §4e drives
#: both of them, and
#: ``test_the_only_api_routes_outside_the_session_gate_are_login_and_the_
#: capability_urls`` fails if the set ever grows without this line changing.
TOKEN_ROUTES = frozenset({"/api/download/{token}", "/api/uploads/{token}"})
#: The committed two-page PDF, shared with ``tests/test_ingest.py``.
PDF_FIXTURE = Path(__file__).parent / "fixtures" / "sample.pdf"

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

    def __init__(self, app, session: str | None = None) -> None:
        self.app = app
        self.session = session

    def request(self, method: str, path: str, guard: bool = True, **kwargs) -> httpx.Response:
        """Issue one request and return the response."""
        headers = {}
        if self.session is not None:
            # A logged-in client offers the session cookie on every request,
            # exactly as a browser would.
            headers["Cookie"] = f"{http_api.SESSION_COOKIE}={self.session}"
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
    """A client with a logged-in owner session over the fresh test database."""
    return _session_client(http_api.create_app())


def _login(app, name: str = "owner", password: str = OWNER_PASSWORD) -> str:
    """Log in over HTTP and return the new session id."""
    response = Client(app).post("/api/login", json={"name": name, "password": password})
    assert response.status_code == 200, response.text
    return response.cookies[http_api.SESSION_COOKIE]


def _session_client(
    app, name: str = "owner", password: str = OWNER_PASSWORD, human_id: str | None = None
) -> Client:
    """A logged-in client over ``app`` (setting the account's password first).

    ``human_id`` defaults to the login name — right for the seeded owner, and
    named explicitly for CLI-created humans whose ids are random.
    """
    service.set_human_password(human_id or name, password, principal=owner())
    return Client(app, session=_login(app, name, password))


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
    rows = service.list_events(limit=500, principal=owner())
    return [row for row in rows if op is None or row.op == op]


# ── 1. Envelope parity: the CLI and the API emit the same bytes ───────────────


def test_node_get_is_byte_identical_to_the_cli(client, fresh_db):
    """Literal byte parity, multibyte content included (both sides keep UTF-8)."""
    node = service.create_node(
        type="note",
        title="École — théorie des graphes",
        content="Un résumé « précis » avec des ✓ et une flèche →.",
        props={"clé": "valeur"},
        principal=owner(),
    )
    result = runner.invoke(cli.app, ["node", "get", node.id, "--as", "owner"])
    assert result.exit_code == 0, result.output

    response = client.get(f"/api/nodes/{node.id}")
    assert response.status_code == 200
    assert response.content == result.stdout.encode("utf-8")
    assert "École" in response.text  # not École: ensure_ascii=False on both sides


@pytest.mark.parametrize(
    ("http_path", "cli_args"),
    [
        ("/api/nodes", ["node", "list", "--as", "owner"]),
        ("/api/edges", ["edge", "list", "--as", "owner"]),
        ("/api/events", ["events", "--as", "owner"]),
        ("/api/humans", ["human", "list", "--as", "owner"]),
        ("/api/agents", ["agent", "list", "--as", "owner"]),
        ("/api/grants", ["grants", "--as", "owner"]),
        ("/api/spaces", ["space-list", "--as", "owner"]),
        # The journal listing. Both sides are one `list_envelope("cycles", …)`
        # over `service.list_cycles` with the same default limit of 50; it was
        # left out of this sweep only because the CLI command's name was still
        # unsettled when the route landed, and it has been `cycle-list` since.
        ("/api/cycles", ["cycle-list", "--as", "owner"]),
    ],
)
def test_list_envelopes_are_byte_identical_to_the_cli(client, fresh_db, http_path, cli_args):
    service.create_node(type="concept", title="Graph Theory", principal=owner())
    service.create_node(
        type="note",
        title="Note",
        content="about [[Graph Theory]]",
        principal=owner(),
    )
    closed = service.open_cycle(trigger="manual", principal=owner())
    service.close_cycle(closed.id, status="completed", report={"jobs": []}, principal=owner())

    result = runner.invoke(cli.app, cli_args)
    assert result.exit_code == 0, result.output

    response = client.get(http_path)
    assert response.status_code == 200
    assert response.content == result.stdout.encode("utf-8")
    payload = response.json()
    key = next(name for name in payload if name != "count")
    assert payload["count"] == len(payload[key])
    # Byte parity over two empty lists proves nothing, so every endpoint in the
    # sweep has something to render.
    assert payload["count"] >= 1, http_path


def test_every_list_endpoint_uses_the_named_key_plus_count(client, fresh_db):
    node = service.create_node(type="note", title="Envelope", content="x", principal=owner())
    service.create_node(type="note", title="Child", parent_id=node.id, principal=owner())
    service.create_node(type="note", title="Proposed", principal=agent(AGENT))

    for path, key in [
        ("/api/nodes", "nodes"),
        (f"/api/nodes/{node.id}/children", "nodes"),
        (f"/api/nodes/{node.id}/history", "versions"),
        ("/api/edges", "edges"),
        ("/api/links/suggest?prefix=", "nodes"),
        ("/api/review/queue", "proposals"),
        ("/api/assets", "assets"),
        ("/api/events", "events"),
        ("/api/humans", "humans"),
        ("/api/agents", "agents"),
        ("/api/grants", "grants"),
        ("/api/spaces", "spaces"),
        ("/api/cycles", "cycles"),
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


def test_a_database_that_is_not_a_database_is_not_a_500(tmp_path, fresh_db, monkeypatch):
    """``sqlite3.DatabaseError`` was unmapped; the CLI prints it and exits 1.

    The session is stubbed at the auth boundary (a verified principal without
    a database round-trip): there is no logging in against a file that is not
    a database, and the mapping under test is the service layer's failure,
    not the middleware's.
    """
    monkeypatch.setattr(
        auth,
        "principal_for_session",
        lambda session_id, *, path=None: Principal(kind="human", id="owner"),
    )
    not_a_db = tmp_path / "notes.txt"
    not_a_db.write_text("this is not a SQLite file", encoding="utf-8")
    client = Client(http_api.create_app(db_path=not_a_db), session="any")

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

    ``docs/architecture.md`` records the first cut of the table claiming to
    hold "exactly the ones ``cli._run`` catches" — the module docstring says
    the same today. It did not: ``cli._run`` catches ``sqlite3.Error``
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


def _package_modules() -> list[ModuleType]:
    """Every module this package contains, sub-packages included.

    The package's own ``__init__`` is first, then everything under it. Both
    halves were missing and both matter: ``_is_domain_failure`` exempts any
    class whose root package is ``nodum``, which covers ``nodum/__init__.py``
    and any future ``nodum.<sub>.<module>`` — while ``pkgutil.iter_modules``
    skips the first and does not descend into the second.
    """
    modules = [nodum]
    for found in pkgutil.walk_packages(nodum.__path__, prefix=f"{nodum.__name__}."):
        modules.append(importlib.import_module(found.name))
    return modules


def _package_exception_classes() -> list[type[BaseException]]:
    """Every exception class this package defines, found by walking the package.

    Discovery, never a literal list: the defect below is a hand-maintained
    exemption that nothing audited, and a test restating the same names by hand
    would have missed the second case exactly as the code did.

    The membership test is ``_is_domain_failure``'s own — the module's *root*
    package, not a ``nodum.`` prefix — so the audit's reach is the exemption's
    reach rather than a near-miss of it.
    """
    root = nodum.__name__
    classes: dict[str, type[BaseException]] = {}
    for module in _package_modules():
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and issubclass(value, BaseException)
                and value.__module__.partition(".")[0] == root
            ):
                classes[f"{value.__module__}.{value.__qualname__}"] = value
    return sorted(classes.values(), key=lambda cls: cls.__name__)


def test_the_exception_walk_covers_every_module_the_exemption_does():
    """The audit's reach is checked against the filesystem, not against pkgutil.

    `_is_domain_failure` exempts a class by its root package, so a class defined
    in `nodum/__init__.py` or in a sub-package `nodum.foo.bar` is exempt from the
    storage-error rewrite the day it is written. The walk under it read one level
    of `nodum/` and skipped `__init__.py`, so either would have been exempt at
    runtime and invisible to the audit — complete for today's flat tree and
    silently wrong for the next one.

    Every `.py` under the package directory is the reference, because that is
    what a future sub-package looks like before anyone remembers this test.
    """
    package_root = Path(nodum.__path__[0])
    on_disk = {
        ".".join(
            (nodum.__name__, *source.relative_to(package_root).with_suffix("").parts)
        ).removesuffix(".__init__")
        for source in package_root.rglob("*.py")
    }
    assert len(on_disk) > 1, "the filesystem reference found nothing, so it proves nothing"

    walked = {module.__name__ for module in _package_modules()}
    assert on_disk <= walked, f"modules outside the walk: {sorted(on_disk - walked)}"


def _mapped_row(exc_type: type[BaseException]) -> type[BaseException] | None:
    """The :data:`http_api.EXCEPTION_STATUS` row Starlette would resolve, by MRO."""
    return next((base for base in exc_type.__mro__ if base in http_api.EXCEPTION_STATUS), None)


def test_no_exception_this_package_defines_is_rewritten_as_a_storage_failure():
    """The ``OSError`` subtree, enumerated — not exempted one class at a time.

    ``_failure_message`` rewrites an ``OSError`` as ``storage error: …`` because
    the CLI's own line names the database file and this surface must not. Three
    of this package's exceptions are ``PermissionError`` subclasses and so fell
    into that net, and the exemption was a literal tuple that nothing audited:
    ``PrincipalDisabled`` was added when a live pass found it, and
    ``GrantNotPermitted`` — the gardener's "you hold no grant on space
    'research', run ``nodum grant …``" — was still being rendered as
    ``storage error: GrantNotPermitted`` on the one surface a human reads it,
    with the space and the remedy both gone. Two misses out of three is the
    exemption list itself failing, so the rule is inverted: a class this package
    defines is a decision it made, never a storage failure, and only an
    ``OSError`` from somewhere else is rewritten.

    Both halves are asserted here — the message survives, and the class carries a
    row of its own rather than inheriting ``OSError``'s 500, since "the server's
    own storage failed" is the wrong status for every one of them.
    """
    domain = _package_exception_classes()
    assert {cls.__name__ for cls in domain} >= {"GrantNotPermitted", "RecordNotFound"}, (
        "the package walk found nothing, so the enumeration below proves nothing"
    )

    in_the_net = [cls for cls in domain if issubclass(cls, OSError)]
    assert in_the_net, "no domain exception derives from OSError — has the subtree moved?"

    sentence = "the gardener holds no grant on space 'research'"
    for exc_type in in_the_net:
        assert http_api._failure_message(exc_type(sentence)) == sentence, (
            f"{exc_type.__name__} is rewritten as a storage failure"
        )
        row = _mapped_row(exc_type)
        assert row is not None and row is not OSError, (
            f"{exc_type.__name__} has no EXCEPTION_STATUS row and inherits OSError's 500"
        )

    # The rewrite still happens for the failures it was written for: a real
    # storage error carries no message a human should be shown, and the CLI's
    # own line for it names the path this surface withholds.
    denied = PermissionError(13, "Permission denied", "/home/someone/nodum.db")
    assert http_api._failure_message(denied) == "storage error: Permission denied"
    assert "nodum.db" not in http_api._failure_message(denied)


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
    assert client.get("/api/assets/deadbeef").status_code == 404
    # A bad enum value is the caller's fault, not a missing record.
    assert client.get("/api/nodes?state=bogus").status_code == 400
    # Archiving twice: the second transition is impossible from `archived`.
    node = service.create_node(type="note", title="Twice", principal=owner())
    assert client.post(f"/api/nodes/{node.id}/archive").status_code == 200
    repeat = client.post(f"/api/nodes/{node.id}/archive")
    assert repeat.status_code == 400
    assert repeat.json()["error"]["type"] == "InvalidTransition"

    source = service.create_node(type="note", title="Source", principal=owner())
    destination = service.create_node(type="note", title="Destination", principal=owner())
    edge = service.create_edge(source.id, destination.id, "relates_to", principal=owner())
    archived = client.post(f"/api/edges/{edge.id}/archive")
    assert archived.status_code == 200
    assert archived.json()["id"] == edge.id
    assert archived.json()["state"] == "archived"
    assert archived.json()["valid_to"] is not None
    repeat_edge = client.post(f"/api/edges/{edge.id}/archive")
    assert repeat_edge.status_code == 400
    assert repeat_edge.json()["error"]["type"] == "InvalidTransition"
    event = next(row for row in _events("edge.archive") if row.payload["after"]["id"] == edge.id)
    assert event.actor == OWNER_ACTOR


def test_an_edge_archive_route_cannot_archive_a_node(client, fresh_db):
    node = service.create_node(type="note", title="Not an edge", principal=owner())

    response = client.post(f"/api/edges/{node.id}/archive")

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "EdgeNotFound"
    assert service.get_node(node.id, principal=owner()).state == "active"


def test_an_undo_the_graph_has_grown_past_is_a_409(client, fresh_db):
    parent = service.create_node(type="note", title="Parent", principal=owner())
    service.create_node(type="note", title="Child", parent_id=parent.id, principal=owner())
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


def _handler_endpoints(app) -> list[tuple[str, object]]:
    """Route endpoints the source-reading rails below can actually read.

    Exactly one route on this app does not present a Python function: the MCP
    transport, whose endpoint is an ASGI object the SDK built and
    :func:`nodum.mcp_server.http_surface` spliced in. ``inspect.getsource``
    cannot read it, so the source rails must leave it out.

    **The exclusion is asserted, not shaped.** Skipping "anything that is not a
    function" would quietly excuse the next raw ASGI app somebody attaches, and
    the properties in this file would stop covering it without a single test
    turning red — the failure mode this suite has been bitten by before
    ([[structural-tests-dont-hold-invariants]]). So: at most one opaque
    endpoint, and it must be ``/mcp``.

    What covers that route instead is not nothing. It reads an identity from
    the request *on purpose* — an agent's, from a bearer header — which is why
    it cannot sit under the actor rails written for the human surface. Its own
    guarantees are asserted over the live transport in ``test_mcp_server.py``,
    and the property that matters here is still enforced module-wide by
    ``test_the_only_credential_path_is_the_session``: the agent path is minted
    in :mod:`nodum.mcp_server`, so :mod:`nodum.http_api` still mints no
    principal without a verified session.
    """
    readable: list[tuple[str, object]] = []
    opaque: list[str] = []
    for path, endpoint in _route_endpoints(app):
        if inspect.isfunction(endpoint) or inspect.ismethod(endpoint):
            readable.append((path, endpoint))
        else:
            opaque.append(path)
    assert sorted(opaque) == [mcp_server.MCP_PATH], (
        "only the MCP transport may present a non-Python endpoint; "
        f"found {sorted(opaque)} — a new one must be reviewed, not skipped"
    )
    return readable


def _module_ast() -> ast.Module:
    """Parse the HTTP adapter's own source."""
    return ast.parse(Path(http_api.__file__).read_text(encoding="utf-8"))


#: The write surface: the only service functions a handler must reach through
#: ``_write`` (each takes a ``principal``; reads take one too, but are safe to
#: call directly — the principal binds identity, and reads change nothing).
WRITE_FUNCTIONS = {
    "create_node",
    "update_node",
    "create_edge",
    "propose_edges",
    "transition",
    "undo",
    "accept_proposals",
    "reject_proposals",
    "accept_matching",
    "reject_matching",
    # Account and grant administration (the /api/humans|agents|grants writers).
    "create_human",
    "set_human_password",
    "disable_human",
    "enable_human",
    "create_agent",
    "rotate_agent_token",
    "disable_agent",
    "enable_agent",
    "grant",
    "revoke",
    # Space lifecycle (the /api/spaces writers). Thin delegates in the service,
    # but writes all the same: each one ends in a node create, update, or
    # transition attributed to whoever called it.
    "create_space",
    "rename_space",
    "archive_space",
    # The curative tier (design §8.2). No route reaches these yet, and listing
    # them before one does is the point: they are the heaviest writes in the
    # system — a merge rewrites a node's state and every edge incident to it —
    # so the rail that says "route it through `_write` and never import it
    # here" has to cover them from the moment they exist, not from the moment
    # somebody adds the handler. `bulk_relink`'s `dry_run` is a read, but the
    # function is a writer.
    "merge_nodes",
    "retype",
    "supersede_edge",
    "bulk_relink",
    # Consolidation cycles. `rollback_cycle` is reached by
    # `POST /api/cycles/{id}/rollback` and is the heaviest write on the surface
    # — it writes recorded payloads back verbatim, `state = 'active'` included,
    # across spaces, for a whole cycle at once. `open_cycle`/`close_cycle` are
    # here for the reason the curative tier was listed before it had a route:
    # they write the journal row, and a handler that opened a cycle of its own
    # would be inventing a second lifecycle beside the runner's.
    # `abandon_cycle` is listed here before its route for the same reason: it
    # closes somebody else's interrupted run and is what makes that run's whole
    # set of writes reversible, so it is a journal write and human-only.
    "open_cycle",
    "close_cycle",
    "abandon_cycle",
    "rollback_cycle",
}


#: Write (method, path-template) pairs this sweep deliberately does not enter,
#: each with the reason. The reachability assertion fails a route that leaves
#: the sweep's reach, so anything listed here is a route the sweep *cannot*
#: enter, not one it happens not to. Empty today: the sweep reaches every write
#: route (a 400 the handler's own validation produces counts as entered — the
#: handler ran). The candidates the review floated were decided as follows:
#: ``POST /api/ask`` and ``POST /api/summarize`` are entered on the missing
#: ``question``/``node_id`` 400 without needing an LLM provider (and write
#: nothing by design, E1); ``POST /api/ingest`` is entered on the neither-path-
#: nor-url 400 *and* runs a real ingest via :data:`PDF_FIXTURE`. A route added
#: later that genuinely cannot be reached here (one whose handler needs an
#: external service no body can fake, say) must be listed here with its reason.
UNREACHABLE_BY_DESIGN: frozenset[tuple[str, str]] = frozenset()


def writable_pairs(app) -> set[tuple[str, str]]:
    """Every state-changing (method, path-template) pair in the live route table.

    The route table is the input, so a write route added tomorrow joins the
    reachability assertion without anyone remembering it exists. The catch-all
    ``/api/{path:path}`` is excluded: it claims every method and refuses
    everything, and a sweep that "entered" it would have entered nothing.
    """
    pairs = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/") or path == "/api/{path:path}":
            continue
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            if method in (route.methods or ()):
                pairs.add((method, path))
    return pairs


def _swept_requests(
    app, ids: dict[str, str], name: str, password: str
) -> list[tuple[str, str, int]]:
    """Fire an actor-carrying request at every method of every route in ``app``.

    The route table is the input, so a handler added later is swept without
    anyone remembering it exists. Every write route is fired with a body shape
    its handler can act on (a real id per route family, real create bodies,
    a real asset hash, live capability tokens) — the whole point of the sweep
    is that the handler runs, because a request refused before the handler
    proves nothing about attribution. A route the sweep cannot enter is a
    failure (see the reachability assertion in the test), not a silence.

    The client drives behind a real session for ``name`` — and re-logs in
    whenever a request kills it (``POST /api/logout`` is in the table too),
    so every route is reached authenticated.

    Every method is swept, reads included: a ``GET`` handler that writes (the
    rendition cache is one) is exactly the case an attribution guarantee must
    not assume away. Each write method is also fired as **multipart** with a
    real image part, because ``POST /api/assets`` is the one route a JSON body
    cannot reach — the origin guard 415s it before the handler, which used to
    leave ``upload_asset`` outside the sweep entirely (Q13 review N9).
    """
    client = Client(app, session=_login(app, name, password))
    bodies = [
        # A bare actor claim: reaches any handler that forwards a whole body.
        {"actor": AGENT},
        # A valid node create — the shape the M1 boundary accepts (extra=forbid
        # refuses the actor claim in the body, so it rides the query string and
        # the header instead).
        {"type": "note", "title": "swept", "content": "swept"},
        # A node update: a title-only PATCH.
        {"title": "swept"},
        # An edge create: real node ids and a real edge type.
        {"src_id": ids["node"], "dst_id": ids["other"], "type": "relates_to"},
        # A review batch: a real proposal id, plus the mandatory reject reason.
        {"ids": [ids["proposal"]], "reason": "swept"},
        # An account or space create.
        {"name": "swept"},
        # A password change.
        {"password": "swept-password"},
        # A grant (the revoke route reads the same agent/space keys).
        {"agent": ids["agent"], "space": ids["space"], "level": "read"},
        # An upload-grant mint.
        {"name": "swept.png", "mime": "image/png", "size": 1000},
        # A real server-side ingest of the committed PDF fixture.
        {"path": str(PDF_FIXTURE), "actor": AGENT},
    ]
    fired: list[tuple[str, str, int]] = []

    # Route templates use `{id}` generically; which real id substitutes is a
    # per-family decision. The blanket node-id replacement this loop once made
    # sent every account/agent/space/cycle/asset route to a 404 before its
    # handler — the exact reach gap this sweep exists to close. The prefix
    # decides the family; a route in none of them takes the node id.
    def concrete(path: str) -> str:
        for prefix, value in (
            ("/api/humans", ids["human"]),
            ("/api/agents", ids["agent"]),
            ("/api/spaces", ids["space"]),
            ("/api/cycles", ids["cycle"]),
            ("/api/assets", ids["asset"]),
            ("/api/edges", ids["edge"]),
        ):
            if path.startswith(prefix):
                path = path.replace("{id}", value)
                break
        else:
            path = path.replace("{id}", ids["node"])
        if "{token}" in path:
            token = (
                ids["upload-token"] if path.startswith("/api/uploads") else ids["download-token"]
            )
            path = path.replace("{token}", token)
        return (
            path.replace("{type}", "note")
            .replace("{agent}", AGENT)
            .replace("{profile}", "thumb")
            .replace("{path:path}", "swept")
        )

    for route in app.routes:
        path = concrete(route.path)
        for method in sorted(route.methods or set()):
            if method in ("GET", "HEAD", "OPTIONS"):
                response = client.request(
                    method, f"{path}?actor={AGENT}", headers={"X-Actor": AGENT}
                )
                if response.status_code == 401:
                    client = Client(app, session=_login(app, name, password))
                    response = client.request(
                        method, f"{path}?actor={AGENT}", headers={"X-Actor": AGENT}
                    )
                # The template path, not the concrete one: the reachability
                # assertion compares against the route table's own templates.
                fired.append((method, route.path, response.status_code))
                continue
            # One part only: the upload handler caps multipart fields at one,
            # so the actor claim rides the filename, the query and the header.
            multipart_kwargs = {
                "files": {"file": (f"{AGENT}.png", _png_bytes(), "image/png")},
                "headers": {"X-Actor": AGENT},
            }
            multipart = client.request(method, f"{path}?actor={AGENT}", **multipart_kwargs)
            if multipart.status_code == 401:
                client = Client(app, session=_login(app, name, password))
                multipart = client.request(method, f"{path}?actor={AGENT}", **multipart_kwargs)
            fired.append((method, f"{route.path} (multipart)", multipart.status_code))
            for body in bodies:
                response = client.request(
                    method, f"{path}?actor={AGENT}", json=body, headers={"X-Actor": AGENT}
                )
                if response.status_code == 401:
                    # The sweep just hit /api/logout (or expired its own
                    # session): log back in and fire again, so the route
                    # after it is still reached authenticated.
                    client = Client(app, session=_login(app, name, password))
                    response = client.request(
                        method, f"{path}?actor={AGENT}", json=body, headers={"X-Actor": AGENT}
                    )
                fired.append((method, route.path, response.status_code))
    return fired


def test_writes_are_attributed_to_the_sessions_human_and_nothing_else(fresh_db, tmp_path):
    """The session-attribution guarantee, as one property over the route table.

    This is the load-bearing test, and it knows nothing about how the boundary
    is implemented — not ``_write``, not the middleware, not which endpoints
    exist. It drives every state-changing method of every route behind a
    **second human's** session with bodies, query strings and headers that
    all claim an agent identity, and then asks the database one question:
    did anything written during that sweep end up attributed to something
    other than the session's human?

    Every AST test in this file was evadable. A handler as short as::

        from nodum.service import create_node as _service_create_node
        async def quick_create(request):
            return EnvelopeResponse(envelope(_service_create_node(**body, path=db_path)))

    named no actor (so the source scan passed), bound no ``principal=``
    keyword (a ``**`` unpack has ``arg=None``, so the binding count passed),
    and called no ``service.<name>`` attribute (so the direct-call scan
    passed) — while ``POST`` with ``{"actor": "agent:evil"}`` produced
    ``created_by: "agent:evil"``. This test fails on it, because the row it
    writes is in the same database the assertion reads.

    **It only proves anything about a route it actually entered** — a request
    refused before the handler (404 on a wrong-id family, a 400 on a body the
    handler never saw) writes nothing and proves nothing. The sweep therefore
    seeds a real id per route family — a second human for ``/api/humans``, an
    agent for ``/api/agents``, a space for ``/api/spaces``, a cycle for
    ``/api/cycles``, an asset hash for ``/api/assets``, live capability tokens
    for the two token routes — plus a body shape each create route accepts, so
    the handlers actually run. The reachability assertion then derives the
    required set from the live route table itself: **every** write route must
    be entered, or the test fails. The integer floors it replaces could not
    see a sweep that lost every write route — the five required successes
    could all be ``GET``s.

    **Two principals besides the session's human may appear, and each is
    named.** ``POST /api/cycles`` asks the consolidation runner to run, and the
    runner writes as the in-process gardener because the gardener made those
    edits (decision G4); ``assets.EXTRACT_ACTOR`` (``"system"``) stamps the
    ``asset.extract`` event an ingest's extraction writes, and is a module
    constant no request can name. Both are *domain* facts, not something the
    request could choose: the sweep therefore keeps asking its question of the
    thing a request could actually influence — who **asked** — and asserts that
    every journal entry the sweep produced records the session's human (or one
    of the two fixed actors) as the trigger, never the agent identity every
    body, query and header claimed. The hole that the gardener exemption could
    open is closed from the other side by
    ``test_the_only_credential_path_is_the_session``, which forbids this module
    from minting the gardener itself.
    """
    app = http_api.create_app()
    second = service.create_human("second", principal=owner())
    service.set_human_password(second.id, "second-pw", principal=owner())
    second_actor = f"human:{second.id}"
    node = service.create_node(type="concept", title="Sweep target", principal=owner())
    other = service.create_node(type="concept", title="Sweep other", principal=owner())
    proposal = service.create_node(type="note", title="Sweep proposal", principal=agent(AGENT))
    # Real ids of the right kind per route family: a node id in any of these
    # slots is a 404 before the handler, which is precisely what the sweep is
    # here to notice (review B10). The tokens are minted as the sweep's second
    # human so the capability routes attribute their writes inside the allowed
    # set. The cycle is `curative`, not a consolidation trigger, so the sweep's
    # own `POST /api/cycles` runs can still open beside it.
    target = service.create_human("target", principal=owner())
    swept_agent = agent("swept-agent")
    space = service.create_space("sweep-seed", principal=owner())
    edge = service.create_edge(node.id, other.id, "relates_to", principal=owner())
    cycle = service.open_cycle(trigger="curative", principal=owner())
    seed_file = tmp_path / "seed.txt"
    seed_file.write_text("sweep asset bytes", encoding="utf-8")
    seeded_asset = assets.register_asset(seed_file, name="seed.txt")
    second_principal = auth.principal_from_actor(second_actor)
    download = urls.mint_download(seeded_asset.hash, principal=second_principal)
    upload = urls.mint_upload(
        "swept.bin", "application/octet-stream", 32 * 1024, principal=second_principal
    )
    assert upload.grant is not None, "a mint without sha256 always grants a URL"
    ids = {
        "node": node.id,
        "other": other.id,
        "proposal": proposal.id,
        "human": target.id,
        "agent": swept_agent.id,
        "space": space.id,
        "edge": edge.id,
        "cycle": cycle.id,
        "asset": seeded_asset.hash,
        "download-token": download.token,
        "upload-token": upload.grant.token,
    }

    before_seq = max((event.seq for event in service.list_events(owner(), limit=5000)), default=0)
    before_nodes = {row.id for row in service.list_nodes(principal=owner(), limit=5000)}
    before_edges = {row.id for row in service.list_edges(principal=owner(), limit=5000)}
    before_cycles = {entry.id for entry in service.list_cycles(limit=5000, principal=owner())}

    fired = _swept_requests(app, ids, "second", "second-pw")

    # The sweep's own upload token is spent by the *refused* multipart attempt
    # (a refusal still spends the token by design), and the spend is logged as
    # an `asset.upload` event — so a redemption that never ingests is already
    # inside the allowed-set check above. What is not: a *successful*
    # redemption, whose describing nodes are written by the re-minted grant
    # principal. Redeem a fresh token with bytes the type policy admits, and
    # pin both ends — the audit event and the write itself.
    redeemed = urls.mint_upload("redeemed.txt", "text/plain", 32, principal=second_principal).grant
    assert redeemed is not None, "a mint without sha256 always grants a URL"
    redemption = Client(app).put(redeemed.url, guard=False, content=b"redeemed by the sweep")
    assert redemption.status_code == 200, redemption.text
    redeemed_envelope = redemption.json()
    # The request carried no session and claimed no identity, so the write is
    # attributed to the grant's minting principal, the sweep's second human —
    # never to the owner, never to the agent identity every other request in
    # the sweep claimed.
    assert redeemed_envelope["source"]["created_by"] == second_actor
    assert redeemed_envelope["asset_ref"]["created_by"] == second_actor
    redeem_uploads = [
        event
        for event in service.list_events(owner(), limit=5000)
        if event.seq > before_seq and event.op == "asset.upload"
    ]
    assert [event.actor for event in redeem_uploads] == [second_actor, second_actor], (
        "both upload-token redemptions of this sweep — the refused attempt and "
        "the successful one — are attributed to the grant's minting principal"
    )

    # Reachability, derived from the route table rather than guessed: every
    # write route must have at least one fire that got past the router and the
    # middleware into the handler. A 400 the handler's own validation produces
    # counts as entered (the handler ran); 404/405 mean the request never
    # reached the handler (a wrong-id family, or a verb the route does not
    # declare), 401/415 are the session gate and the media-type guard, and a
    # 5xx is a handler that crashed — all of them fail the sweep.
    entered = {
        (method, path.removesuffix(" (multipart)"))
        for method, path, status in fired
        if status < 500 and status not in (401, 404, 405, 415)
    }
    missing = writable_pairs(app) - entered - UNREACHABLE_BY_DESIGN
    assert missing == set(), f"the sweep never reached these write handlers: {sorted(missing)}"
    # And the multipart pass really did register an asset, rather than 415ing
    # at the guard the way the JSON bodies do (review N9).
    assert any(path.endswith("(multipart)") and status < 300 for _, path, status in fired), fired

    new_events = [
        event for event in service.list_events(owner(), limit=5000) if event.seq > before_seq
    ]
    assert new_events, "the sweep wrote nothing, so it proves nothing"
    allowed = {second_actor, GARDENER_ACTOR, assets.EXTRACT_ACTOR}
    offenders = [
        (event.op, event.seq, event.actor) for event in new_events if event.actor not in allowed
    ]
    assert offenders == [], f"writes attributed outside the session's human: {offenders}"

    written_nodes = [
        row
        for row in service.list_nodes(principal=owner(), limit=5000)
        if row.id not in before_nodes
    ]
    written_edges = [
        row
        for row in service.list_edges(principal=owner(), limit=5000)
        if row.id not in before_edges
    ]
    assert {row.created_by for row in written_nodes} <= allowed
    assert {row.created_by for row in written_edges} <= allowed

    # The gardener exemption above is only sound if a request cannot say who
    # asked for the cycle. The sweep ran some — it claimed an agent identity in
    # every body, query string and header while doing it — and every journal
    # entry it produced still records the session's human.
    swept_cycles = [
        entry
        for entry in service.list_cycles(limit=5000, principal=owner())
        if entry.id not in before_cycles
    ]
    assert swept_cycles, "the sweep opened no cycle, so the gardener exemption proves nothing"
    assert {entry.triggered_by for entry in swept_cycles} == {second_actor}


def test_every_api_route_but_login_refuses_a_request_without_a_session(fresh_db):
    """The no-session sweep: one 401 per method per route, from the live table.

    Reads included — the plan leaves the choice and this app takes the simple
    one: a single-human file has nothing an anonymous caller should see, and
    one rule ("every ``/api`` route but login") is the one no future endpoint
    can forget. The route table is the input, so a route added tomorrow is
    gated or this fails.

    :data:`TOKEN_ROUTES` is the one deliberate hole in the rule, and it is a
    hole in *this test* only because those two routes carry their own
    credential — §4e drives them, unauthenticated, against every way they can
    be refused.
    """
    app = http_api.create_app()
    anonymous = Client(app)

    outcomes: list[tuple[str, str, int]] = []
    for route in app.routes:
        path = route.path
        if not path.startswith("/api/") or path == "/api/login" or path in TOKEN_ROUTES:
            continue
        concrete = (
            path.replace("{id}", "x")
            .replace("{type}", "note")
            .replace("{profile}", "thumb")
            .replace("{path:path}", "x")
        )
        for method in sorted(route.methods or set()):
            if method in ("HEAD", "OPTIONS"):
                continue
            if concrete == "/api/assets" and method == "POST":
                # The one multipart route: anything else the origin guard
                # refuses (415) before the session gate even runs.
                response = anonymous.post(
                    concrete, files={"file": ("x.png", _png_bytes(), "image/png")}
                )
            elif method == "GET":
                response = anonymous.get(concrete)
            else:
                response = anonymous.request(method, concrete, json={})
            outcomes.append((method, concrete, response.status_code))

    assert len(outcomes) >= 30, outcomes
    not_refused = [(method, path, status) for method, path, status in outcomes if status != 401]
    assert not_refused == [], f"routes reachable without a session: {not_refused}"

    # The open set, asserted positively: the probe, the login route, and the
    # page that holds the login form.
    assert anonymous.get("/healthz").status_code == 200
    assert anonymous.get("/").status_code == 200
    bad_login = anonymous.post("/api/login", json={"name": "owner", "password": "nope"})
    assert bad_login.status_code == 401  # refused by the handler, not the gate
    assert bad_login.json()["error"]["type"] == "InvalidCredentials"


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
    classes = [entry.cls for entry in http_api.create_app().user_middleware]
    assert classes == [http_api.RequestGuardMiddleware, http_api.SessionMiddleware]


def test_no_route_handler_can_read_an_actor_from_a_request(fresh_db):
    """Enumerated absence over the live route table: no handler names an actor.

    Cheap, and it catches the obvious version of the mistake — a handler that
    reads ``request.query_params["actor"]``. It does **not** catch a handler
    that forwards a body it never inspects, which is why it is no longer the
    test the guarantee rests on.
    """
    endpoints = _handler_endpoints(http_api.create_app())
    offenders = [
        path for path, endpoint in endpoints if "actor" in inspect.getsource(endpoint).casefold()
    ]
    assert offenders == [], f"route handlers must never name an actor: {offenders}"


def test_every_principal_binding_mints_through_the_session():
    """Every ``principal=`` in the module is ``_session_principal(request)``.

    Writes bind once (``_write``); reads bind per handler. The rule either
    way: the value is always the principal the session middleware verified
    into this request's scope, so identity can only ever come from a session,
    and a handler that tried to bind request data — or a trusted-local
    principal — as an identity fails here.
    """
    bindings = [
        keyword
        for node in ast.walk(_module_ast())
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "principal"
    ]
    assert bindings, "the module binds at least one principal"
    for value in (keyword.value for keyword in bindings):
        assert (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "_session_principal"
            and len(value.args) == 1
            and isinstance(value.args[0], ast.Name)
            and value.args[0].id == "request"
        ), "every principal= binding must be _session_principal(request)"


def test_the_only_credential_path_is_the_session():
    """The trusted-local and agent-token entries must never mint an HTTP identity.

    ``auth.owner_principal`` is the CLI's no-credential path and
    ``auth.verify_agent_token`` is the MCP one; either in this module would be
    a principal without a verified session. The allowed set is exactly the
    session lifecycle: login, resolve, delete.

    ``internal_principal`` is the newest and now the most load-bearing entry in
    the list: it is the gardener's door, it takes no credential because there is
    none to take, and the attribution sweep above deliberately tolerates writes
    made under it. That tolerance is only safe while the gardener can be minted
    by the *domain* alone — the runner and the scheduler — and never by a
    handler here. ``principal_from_actor`` sits beside it for the same reason:
    minting a principal from a string is what the runner does with the string
    this surface hands it, one layer down, where the value came from a verified
    session and not from a request.
    """
    forbidden = {
        "owner_principal",
        "agent_principal",
        "verify_agent_token",
        "internal_principal",
        "principal_from_actor",
        "set_password",
        "store_token",
    }
    calls = {
        node.func.attr
        for node in ast.walk(_module_ast())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "auth"
    }
    assert calls & forbidden == set(), (
        f"the HTTP surface must not mint a principal without a session: {sorted(calls & forbidden)}"
    )


#: Values that may be splatted into a call in ``nodum.http_api``. Each one is
#: allowlisted at source, so what it produces cannot contain an identity:
#: ``_proposal_filters``/``_selective_filters`` read only ``PROPOSAL_FILTERS``,
#: ``_search_filters`` writes its own key list and reads nothing else off the
#: query string (it exists because ``?nl=1`` sends the identical filters to a
#: different function, and two hand-written argument lists that must agree are
#: two that will not), ``fields`` is the ``PATCHABLE_FIELDS`` comprehension in
#: ``update_node``, and ``kwargs`` is ``_write``'s own forward — the one place
#: that *does* receive a caller's dict wholesale, and the one that refuses an
#: ``actor`` key outright
#: (``test_the_write_helper_refuses_a_caller_supplied_actor``).
ALLOWED_UNPACK_SOURCES = {
    "_proposal_filters",
    "_search_filters",
    "_selective_filters",
    "fields",
    "kwargs",
}


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
    writers = WRITE_FUNCTIONS
    missing = [
        name
        for name in writers
        if "principal" not in inspect.signature(getattr(service, name)).parameters
    ]
    assert not missing, f"write functions must take a principal: {missing}"
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


def test_the_write_helper_refuses_a_caller_supplied_principal(fresh_db):
    """The backstop: even a wholesale kwargs forward cannot smuggle an identity."""
    with pytest.raises(RuntimeError, match="never takes a principal"):
        http_api._write(None, service.create_node, type="note", principal=agent(AGENT))


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
    proposal = service.create_node(type="note", title="Bot draft", principal=agent(AGENT))
    body = {**smuggled, "ids": [proposal.id]}

    payload = _ok(client.post("/api/review/accept", json=body))

    assert payload["actor"] == OWNER_ACTOR
    assert payload["transitioned"] == [proposal.id]
    accepts = _events("node.accept")
    assert [row.actor for row in accepts] == [OWNER_ACTOR]


def test_a_smuggled_actor_on_a_create_is_refused(client, fresh_db):
    """Property 2 on the create route: a smuggled identity field is refused.

    The create body is validated against an input model with ``extra="forbid"``
    (finding M1), so an ``actor``/``created_by`` key is a 400 — refused rather
    than the "inert" the other routes still are, and never honored: nothing is
    written at all, so nothing can be attributed to the smuggled identity.
    """
    before = _events()
    response = client.post(
        "/api/nodes?actor=agent:query",
        json={"type": "note", "title": "Smuggled", "actor": AGENT, "created_by": AGENT},
        headers={"X-Actor": AGENT},
    )
    assert response.status_code == 400
    # The refusal wrote nothing — no row, no event — so no identity stuck.
    assert service.list_nodes(principal=owner()) == []
    assert _events() == before


def test_an_agent_proposal_accepted_over_http_is_a_human_write(client, fresh_db):
    """Property 3 (end-to-end): propose as an agent, accept over HTTP."""
    target = service.create_node(type="concept", title="Osmosis", principal=owner())
    proposal = service.create_node(
        type="note", title="Agent draft", content="see [[Osmosis]]", principal=agent(AGENT)
    )
    pending_edge = service.list_edges(node_id=proposal.id, principal=owner())[0]
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
    assert accept.actor == OWNER_ACTOR
    assert accept.payload["after"]["state"] == "active"
    # The wikilink edge the agent staged went live under the reviewer's name.
    edge_accept = _events("edge.accept")[0]
    assert edge_accept.actor == OWNER_ACTOR
    assert service.list_edges(node_id=target.id, principal=owner())[0].state == "active"


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
    _ok(client.post("/api/assets", files={"file": ("photo.png", _png_bytes(), "image/png")}))

    accepted = service.create_node(type="note", title="Accept me", principal=agent(AGENT))
    rejected = service.create_node(type="note", title="Reject me", principal=agent(AGENT))
    _ok(client.post("/api/review/accept", json={"ids": [accepted.id]}))
    _ok(client.post("/api/review/reject", json={"ids": [rejected.id], "reason": "off topic"}))

    # Archive, then undo it: the restored row goes back to `active`, which is
    # the write undo makes and the reason it is human-only.
    _ok(client.post(f"/api/nodes/{concept['id']}/archive"))
    _ok(client.post("/api/undo", json={}))
    assert service.get_node(concept["id"], principal=owner()).state == "active"

    live_writes = 0
    for event in service.list_events(limit=500, principal=owner()):
        for row in (event.payload.get("after"), event.payload.get("restored")):
            if isinstance(row, dict) and row.get("state") == "active":
                live_writes += 1
                assert event.actor == OWNER_ACTOR, f"{event.op} at seq {event.seq}"
    # A vacuous pass would be worthless: the run really did write live state.
    assert live_writes >= 6
    assert any(event.actor == AGENT for event in service.list_events(limit=500, principal=owner()))


def test_a_reject_without_a_reason_is_refused(client, fresh_db):
    """The CLI's audit guarantee, mirrored: no reason, no reject."""
    proposal = service.create_node(type="note", title="Bot draft", principal=agent(AGENT))
    for body in ({"ids": [proposal.id]}, {"ids": [proposal.id], "reason": "   "}):
        response = client.post("/api/review/reject", json=body)
        assert response.status_code == 400
        assert "reason" in response.json()["error"]["message"]
    assert service.get_node(proposal.id, principal=owner()).state == "proposed"


def test_a_bodyless_review_refuses_to_touch_the_whole_queue(client, fresh_db):
    service.create_node(type="note", title="Bot draft", principal=agent(AGENT))
    response = client.post("/api/review/accept", json={})
    assert response.status_code == 400
    assert "whole queue" in response.json()["error"]["message"]
    assert len(service.list_proposals(principal=owner())) == 1


# ── 4. Password sessions ─────────────────────────────────────────────────────


def test_login_sets_an_httponly_strict_session_cookie(fresh_db):
    service.set_human_password("owner", OWNER_PASSWORD, principal=owner())
    anonymous = Client(http_api.create_app())

    response = anonymous.post("/api/login", json={"name": "owner", "password": OWNER_PASSWORD})

    assert response.status_code == 200, response.text
    assert response.json() == {"human": "owner"}
    cookie = response.headers["set-cookie"]
    assert f"{http_api.SESSION_COOKIE}=" in cookie
    assert "httponly" in cookie.lower()
    assert "samesite=strict" in cookie.lower()
    assert "path=/" in cookie.lower()
    # Loopback is plain HTTP: a Secure cookie would never be stored at all.
    assert "secure" not in cookie.lower()


def test_a_lan_bind_marks_the_session_cookie_secure(fresh_db):
    service.set_human_password("owner", OWNER_PASSWORD, principal=owner())
    app = http_api.create_app(secure_cookies=True)

    response = Client(app).post("/api/login", json={"name": "owner", "password": OWNER_PASSWORD})

    assert "secure" in response.headers["set-cookie"].lower()


def test_login_failures_are_indistinguishable_401s(fresh_db):
    """Unknown name, wrong password: same status, same body, no cookie."""
    service.set_human_password("owner", OWNER_PASSWORD, principal=owner())
    anonymous = Client(http_api.create_app())

    bodies = []
    for credentials in (
        {"name": "owner", "password": "wrong"},
        {"name": "nobody", "password": OWNER_PASSWORD},
    ):
        response = anonymous.post("/api/login", json=credentials)
        assert response.status_code == 401
        assert "set-cookie" not in response.headers
        bodies.append(response.json())
    assert bodies[0] == bodies[1]
    assert bodies[0]["error"]["type"] == "InvalidCredentials"


def test_a_garbage_session_cookie_is_a_401_not_a_500(fresh_db):
    anonymous = Client(http_api.create_app())

    for cookie in ("no-such-session", "", "x" * 500):
        response = anonymous.get(
            "/api/types", headers={"Cookie": f"{http_api.SESSION_COOKIE}={cookie}"}
        )
        assert response.status_code == 401, cookie
        assert response.json()["error"]["type"] == "Unauthorized"


def test_logout_kills_the_session_and_clears_the_cookie(client, fresh_db):
    session = client.session

    response = client.post("/api/logout")

    assert response.status_code == 200
    cleared = response.headers["set-cookie"]
    assert http_api.SESSION_COOKIE in cleared
    assert "max-age=0" in cleared.lower() or "expires=" in cleared.lower()
    # The row is gone server-side: the same cookie is now worthless.
    assert client.get("/api/types").status_code == 401
    assert Client(http_api.create_app(), session=session).get("/api/types").status_code == 401


def test_a_successful_login_writes_a_human_login_event(fresh_db):
    """The auth half of the audit trail: a successful login is on record.

    M5: the finding was that a password login wrote nothing at all — success
    or failure — to the events table the service calls "the audit trail".
    """
    service.set_human_password("owner", OWNER_PASSWORD, principal=owner())
    anonymous = Client(http_api.create_app())

    response = anonymous.post("/api/login", json={"name": "owner", "password": OWNER_PASSWORD})

    assert response.status_code == 200, response.text
    (login,) = _events("human.login")
    assert login.actor == OWNER_ACTOR
    assert login.payload == {"human_id": "owner"}


def test_a_refused_login_writes_a_human_login_failed_event(fresh_db):
    """Wrong credentials stay an indistinguishable 401 and land on the log.

    The attempted name is in the *payload*; the actor is
    ``UNAUTHENTICATED_ACTOR``, because a failed login has no verified
    principal. The payload carries no password material.
    """
    service.set_human_password("owner", OWNER_PASSWORD, principal=owner())
    anonymous = Client(http_api.create_app())

    response = anonymous.post("/api/login", json={"name": "owner", "password": "wrong"})

    assert response.status_code == 401
    (failed,) = _events("human.login_failed")
    assert failed.actor == service.UNAUTHENTICATED_ACTOR
    assert failed.payload == {"name": "owner", "reason": "invalid credentials"}


def test_an_unauthenticated_caller_cannot_choose_the_actor_a_failed_login_is_logged_under(
    fresh_db,
):
    """`events.actor` is never a string off the wire (finding M2).

    Login is the one `/api` route outside the session gate, and the attempted
    name used to become the event's actor verbatim — so `{"name":
    "human:owner"}` wrote rows attributed to the seeded owner, with no
    credential presented, into the column that answers *who did this*. The
    name is data about the attempt and lives in the payload; the actor is an
    identity, and on a failure the only truthful one is nobody.
    """
    anonymous = Client(http_api.create_app())

    for claimed in ("human:owner", "agent:builtin-gardener", "scheduler"):
        assert (
            anonymous.post("/api/login", json={"name": claimed, "password": "x"}).status_code == 401
        )

    actors = {event.actor for event in _events("human.login_failed")}
    assert actors == {service.UNAUTHENTICATED_ACTOR}
    # The claim is still on the record, where it is evidence rather than identity.
    assert {event.payload["name"] for event in _events("human.login_failed")} == {
        "human:owner",
        "agent:builtin-gardener",
        "scheduler",
    }
    # And it cannot be read back as an account.
    with pytest.raises(auth.UnknownPrincipal):
        auth.principal_from_actor(service.UNAUTHENTICATED_ACTOR)


def test_a_lockout_refusal_writes_no_event_of_its_own(fresh_db):
    """A refused-before-checking attempt is a rate limit, not an audit entry (M2).

    It used to write one, so that a guesser who kept trying kept the lockout
    fresh. That is two defects at once: an unauthenticated request became an
    unbounded append to the append-only log, and any local process could hold
    the real human out forever by re-arming the window every quarter-hour. The
    five failures that *earned* the lockout are the record; their refusals are
    not.
    """
    anonymous = Client(http_api.create_app())
    for _ in range(service.LOGIN_MAX_FAILED_ATTEMPTS):
        anonymous.post("/api/login", json={"name": "owner", "password": "wrong"})
    assert len(_events("human.login_failed")) == service.LOGIN_MAX_FAILED_ATTEMPTS

    for _ in range(20):
        assert (
            anonymous.post("/api/login", json={"name": "owner", "password": "wrong"}).status_code
            == 429
        )

    assert len(_events("human.login_failed")) == service.LOGIN_MAX_FAILED_ATTEMPTS


def test_five_failed_attempts_lock_a_name_until_the_window_slides(fresh_db):
    """M5: the lockout refuses the next attempt — correct password or not.

    The window is real: once it slides past the failures, the correct password
    works again. The refusal writes no event of its own (M2) — see
    ``test_a_lockout_refusal_writes_no_event_of_its_own``.
    """
    service.set_human_password("owner", OWNER_PASSWORD, principal=owner())
    anonymous = Client(http_api.create_app())
    for _ in range(service.LOGIN_MAX_FAILED_ATTEMPTS):
        assert (
            anonymous.post("/api/login", json={"name": "owner", "password": "wrong"}).status_code
            == 401
        )

    refused = anonymous.post("/api/login", json={"name": "owner", "password": OWNER_PASSWORD})
    assert refused.status_code == 429
    assert refused.json()["error"]["type"] == "LoginLocked"
    assert service.login_is_locked("owner") is True

    conn = db.connect()
    try:
        conn.execute(
            "UPDATE events SET created_at = datetime('now', '-30 minutes')"
            " WHERE op = 'human.login_failed'"
        )
        conn.commit()
    finally:
        conn.close()
    assert service.login_is_locked("owner") is False
    assert (
        anonymous.post("/api/login", json={"name": "owner", "password": OWNER_PASSWORD}).status_code
        == 200
    )


def test_the_lockout_is_not_an_existence_oracle(fresh_db):
    """A nonexistent name locks exactly like a real one — no way to probe.

    The failed attempts are counted by the name the attempt claimed, so
    nothing about the answer discloses whether the name exists.
    """
    anonymous = Client(http_api.create_app())
    for _ in range(service.LOGIN_MAX_FAILED_ATTEMPTS):
        assert (
            anonymous.post("/api/login", json={"name": "nobody", "password": "wrong"}).status_code
            == 401
        )

    refused = anonymous.post("/api/login", json={"name": "nobody", "password": "anything"})
    assert refused.status_code == 429
    assert refused.json()["error"]["type"] == "LoginLocked"


def test_a_successful_login_resets_the_failure_count(fresh_db):
    """Consecutive misses are real: a success clears the failures before it.

    Three misses, a correct login, three more misses: only the last three
    count, so the name is not locked — without the reset it would be six
    failures and the seventh attempt would be refused.
    """
    service.set_human_password("owner", OWNER_PASSWORD, principal=owner())
    anonymous = Client(http_api.create_app())
    for _ in range(3):
        assert (
            anonymous.post("/api/login", json={"name": "owner", "password": "wrong"}).status_code
            == 401
        )
    assert (
        anonymous.post("/api/login", json={"name": "owner", "password": OWNER_PASSWORD}).status_code
        == 200
    )
    for _ in range(3):
        assert (
            anonymous.post("/api/login", json={"name": "owner", "password": "wrong"}).status_code
            == 401
        )
    assert (
        anonymous.post("/api/login", json={"name": "owner", "password": OWNER_PASSWORD}).status_code
        == 200
    )


def _counting_verify_login(monkeypatch) -> list[str]:
    """Wrap ``auth.verify_login`` so a test can see whether argon2 ran at all.

    Returns the list the names actually verified land in — empty means the
    handler refused the body before it ever reached the password check.
    """
    verified: list[str] = []
    real = auth.verify_login

    def counting(name, password, **kwargs):
        verified.append(name)
        return real(name, password, **kwargs)

    monkeypatch.setattr(auth, "verify_login", counting)
    return verified


def test_concurrent_logins_never_exceed_the_login_verification_limiter(fresh_db, monkeypatch):
    """The one unauthenticated route may not reserve memory without a bound.

    Moving argon2id off the loop was right and incomplete: inline, exactly one
    verification ran at a time, and through the *default* thread limiter forty
    do — each holding argon2id's 64 MiB memory cost. Sixteen concurrent
    unauthenticated attempts were measured at +1032 MiB of RSS against
    +64 MiB with the call inline, with a ceiling around 2.5 GiB. The per-name
    lockout bounds none of it, which is why every attempt below claims a
    different name: rotating the name never trips it.

    There is no way to assert "the server did not become resident" from inside
    the process, but the mechanism that keeps it from becoming resident is
    assertable — how many verifications are ever in flight at once. Each one
    records its own arrival and departure, so the high-water mark is exact.
    """
    service.set_human_password("owner", OWNER_PASSWORD, principal=owner())
    app = http_api.create_app()
    attempts = 6 * http_api.ARGON2_CONCURRENCY

    lock = threading.Lock()
    in_flight = 0
    peak = 0
    real_verify = auth.verify_login

    def watched_verify(name, password, **kwargs):
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        try:
            # Wide enough that unbounded callers demonstrably overlap; argon2's
            # own ~100 ms would mostly do it, but not as a guarantee.
            time.sleep(0.02)
            return real_verify(name, password, **kwargs)
        finally:
            with lock:
                in_flight -= 1

    monkeypatch.setattr(auth, "verify_login", watched_verify)

    async def hammer() -> list[httpx.Response]:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as concurrent:
            return await asyncio.gather(
                *(
                    concurrent.post(
                        "/api/login",
                        json={"name": f"nobody-{index}", "password": "wrong"},
                        headers=CLIENT_HEADERS,
                    )
                    for index in range(attempts)
                )
            )

    responses = asyncio.run(hammer())

    assert peak <= http_api.ARGON2_CONCURRENCY, (
        f"{peak} argon2 verifications were in flight at once, "
        f"bound is {http_api.ARGON2_CONCURRENCY}"
    )
    assert peak >= 1, "the wrapper never ran: this test asserted nothing"
    # Every one of them was still answered: the excess *queued* on the limiter
    # rather than being refused, which would hand anybody with a socket a way
    # to deny logins to the human this route exists for.
    assert [response.status_code for response in responses] == [401] * attempts


def test_an_over_long_login_name_is_refused_before_the_log_and_before_argon2(fresh_db, monkeypatch):
    """An unauthenticated caller does not get to choose the size of a row.

    The name a failed attempt claimed is written verbatim into the append-only
    ``events`` payload, and nothing capped its length: ``{"name": "A" *
    200_000}`` appended a 200 kB row from a caller with no session, bounded
    only by ``MAX_REQUEST_BYTES`` — 32 MiB, per attempt, indefinitely. The cap
    has to bite in the handler, because the lockout query, argon2 and the
    failure event are all on the far side of it.
    """
    service.set_human_password("owner", OWNER_PASSWORD, principal=owner())
    anonymous = Client(http_api.create_app())
    verified = _counting_verify_login(monkeypatch)

    response = anonymous.post(
        "/api/login", json={"name": "A" * 200_000, "password": OWNER_PASSWORD}
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "ValueError"
    # The refusal does not echo the field back: a message quoting a 200 kB name
    # moves it into the response body and the server log instead of the table.
    assert "AAAAAAAAAA" not in response.text
    assert _events("human.login_failed") == []
    assert not any("AAAAAAAAAA" in json.dumps(event.payload) for event in _events())
    assert verified == []  # argon2 never ran either

    # The boundary is the constant, and a name exactly on it is ordinary input.
    at_cap = anonymous.post(
        "/api/login",
        json={"name": "A" * service.MAX_HUMAN_NAME_LENGTH, "password": OWNER_PASSWORD},
    )
    assert at_cap.status_code == 401
    assert verified == ["A" * service.MAX_HUMAN_NAME_LENGTH]
    assert len(_events("human.login_failed")) == 1


def test_an_over_long_login_password_is_refused_before_argon2(fresh_db, monkeypatch):
    """argon2id hashes whatever it is handed, at the full work factor.

    So an uncapped password field is CPU an unauthenticated caller buys by the
    megabyte — and it is spent on unknown names too, through the constant-time
    dummy path. The concurrency limiter caps how many verifications run at
    once; this caps what one of them can cost.
    """
    service.set_human_password("owner", OWNER_PASSWORD, principal=owner())
    anonymous = Client(http_api.create_app())
    verified = _counting_verify_login(monkeypatch)

    response = anonymous.post("/api/login", json={"name": "owner", "password": "p" * 200_000})

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "ValueError"
    assert "pppppppppp" not in response.text
    assert verified == []
    assert _events("human.login_failed") == []

    # And a password exactly on the cap is verified like any other wrong one.
    at_cap = anonymous.post(
        "/api/login",
        json={"name": "owner", "password": "p" * service.MAX_PASSWORD_LENGTH},
    )
    assert at_cap.status_code == 401
    assert verified == ["owner"]


def test_a_name_and_password_this_surface_can_store_are_ones_it_can_still_log_in(client, fresh_db):
    """The login caps are the *service's*, so no write can outrun them.

    They landed on the read side only, and a cap one end honours is worse than
    no cap: ``POST /api/humans`` took a 300-character name and
    ``POST /api/humans/{id}/password`` took a 5000-character password, both
    answered 200, and the account then met a **400** on its own correct
    credentials — ``field 'name' must be at most 256 characters`` — because the
    login route refused the field before it could look the account up. A
    supported write minted an account nobody could log into.

    The property that closes it is a round trip, not a constant: whatever the
    longest storable name and password are, logging in with them works. It
    holds because both ends read one number
    (:data:`nodum.service.MAX_HUMAN_NAME_LENGTH`,
    :data:`nodum.service.MAX_PASSWORD_LENGTH`) — an adapter-owned copy is how
    they came apart.
    """
    longest_name = "n" * service.MAX_HUMAN_NAME_LENGTH
    longest_password = "p" * service.MAX_PASSWORD_LENGTH

    created = _ok(client.post("/api/humans", json={"name": longest_name}))
    human_id = created["id"]
    assert (
        client.post(
            f"/api/humans/{human_id}/password", json={"password": longest_password}
        ).status_code
        == 200
    )

    logged_in = Client(client.app).post(
        "/api/login", json={"name": longest_name, "password": longest_password}
    )

    assert logged_in.status_code == 200, logged_in.text
    assert logged_in.json()["human"] == human_id

    # One character past either ceiling is refused *at the write*, where the
    # account would otherwise be created, and the value never comes back in the
    # refusal.
    too_long_name = client.post("/api/humans", json={"name": longest_name + "n"})
    assert too_long_name.status_code == 400
    assert too_long_name.json()["error"]["type"] == "ValueError"
    assert "nnnnnnnnnn" not in too_long_name.text
    assert {human.name for human in service.list_humans(principal=owner())} == {
        "owner",
        longest_name,
    }

    too_long_password = client.post(
        f"/api/humans/{human_id}/password", json={"password": longest_password + "p"}
    )
    assert too_long_password.status_code == 400
    assert too_long_password.json()["error"]["type"] == "ValueError"
    assert "pppppppppp" not in too_long_password.text
    # The refused set left the credential that already worked alone.
    assert (
        Client(client.app)
        .post("/api/login", json={"name": longest_name, "password": longest_password})
        .status_code
        == 200
    )


def test_setting_a_password_hashes_off_the_loop_under_the_argon2_limiter(client, fresh_db):
    """The session gate says who may hash, never how much memory hashing may hold.

    This route is the second argon2 caller on the surface and ran **inline**:
    ~100 ms at argon2id's 64 MiB profile on the single-threaded event loop, so
    every other tab stalled for it — and with no limiter, 40 default threads of
    it is the same ~2.5 GiB ``POST /api/login`` is bounded away from, reached by
    an authenticated human instead of an anonymous one.

    Both halves are asserted, because either alone is passable by the old code:
    inline hashing trivially satisfies a concurrency bound (never more than one
    at a time), and a threadpool hop on the *default* limiter satisfies "off the
    loop" while reserving the memory. So the hash must run where no event loop
    does, and several concurrent sets must genuinely overlap — up to
    :data:`nodum.http_api.ARGON2_CONCURRENCY` and never past it.
    """
    humans = [
        _ok(client.post("/api/humans", json={"name": f"human-{index}"}))["id"]
        for index in range(3 * http_api.ARGON2_CONCURRENCY)
    ]

    lock = threading.Lock()
    in_flight = 0
    peak = 0
    on_the_loop: list[str] = []
    real_hash = auth.hash_password

    def watched_hash(password: str) -> str:
        nonlocal in_flight, peak
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            # A running loop in this thread means the hash is holding it.
            on_the_loop.append(password)
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        try:
            # Wide enough that concurrent callers demonstrably overlap; argon2's
            # own ~100 ms would mostly do it, but not as a guarantee.
            time.sleep(0.02)
            return real_hash(password)
        finally:
            with lock:
                in_flight -= 1

    async def hammer() -> list[httpx.Response]:
        transport = httpx.ASGITransport(app=client.app, raise_app_exceptions=False)
        headers = {
            "Cookie": f"{http_api.SESSION_COOKIE}={client.session}",
            **CLIENT_HEADERS,
        }
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as concurrent:
            return await asyncio.gather(
                *(
                    concurrent.post(
                        f"/api/humans/{human_id}/password",
                        json={"password": f"passphrase-{human_id}"},
                        headers=headers,
                    )
                    for human_id in humans
                )
            )

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(auth, "hash_password", watched_hash)
        responses = asyncio.run(hammer())

    assert [response.status_code for response in responses] == [200] * len(humans)
    assert on_the_loop == [], "argon2 ran on the event loop: every other request waited for it"
    assert peak >= 2, "the sets never overlapped: this asserted nothing about the bound"
    assert peak <= http_api.ARGON2_CONCURRENCY, (
        f"{peak} argon2 hashes were in flight at once, bound is {http_api.ARGON2_CONCURRENCY}"
    )


def test_logout_records_the_human_logout_event_and_removes_the_session(client, fresh_db):
    """Logout is the last entry a session writes, and the row is really gone."""
    session = client.session

    response = client.post("/api/logout")

    assert response.status_code == 200
    (logout,) = _events("human.logout")
    assert logout.actor == OWNER_ACTOR
    assert logout.payload == {"human_id": "owner"}
    conn = db.connect()
    try:
        remaining = conn.execute(
            "SELECT count(*) AS n FROM sessions WHERE id = ?",
            (hashlib.sha256(session.encode()).hexdigest(),),
        ).fetchone()["n"]
    finally:
        conn.close()
    assert remaining == 0


def test_a_dead_session_cookie_is_cleared_on_the_401(fresh_db):
    """A client offering an expired/deleted cookie should stop offering it."""
    response = Client(http_api.create_app(), session="no-such-session").get("/api/types")

    assert response.status_code == 401
    assert http_api.SESSION_COOKIE in response.headers["set-cookie"]


def test_disabling_the_human_kills_the_session_at_the_next_request(client, fresh_db):
    service.create_human("second", principal=owner())  # the owner is not the last one
    service.disable_human("owner", principal=owner())

    assert client.get("/api/types").status_code == 401


def test_the_session_gate_and_the_router_agree_on_what_an_api_path_is(fresh_db):
    """``//api/nodes`` used to be an API path to the gate and a SPA path to the router.

    Nothing leaked — both spellings fell through to the SPA — but a gate and a
    router keyed differently is a bug with one half missing. Paths are
    normalised once, at the outermost layer, so ``//api/nodes`` is now an API
    path to *both* and is gated like one.
    """
    anonymous = Client(http_api.create_app())
    # Absolute, because httpx reads a leading `//` in a relative URL as
    # scheme-relative and would rewrite the host out from under the test.
    doubled = f"{BASE_URL}//api/nodes"

    assert anonymous.get(doubled).status_code == 401
    assert anonymous.get(f"{BASE_URL}/api//nodes").status_code == 401
    # A different case is a different path to the router, so it is not an API
    # path to either side — it falls through to the SPA, consistently.
    assert anonymous.get("/API/nodes").status_code == 200
    assert anonymous.get("/api/nodes").status_code == 401

    logged_in = _session_client(http_api.create_app())
    assert logged_in.get(doubled).status_code == 200


# ── 4b. Origin control: the browser cannot drive this API ─────────────────────


CSRF_WRITES = [
    ("POST", "/api/review/accept"),
    ("POST", "/api/undo"),
    ("POST", "/api/nodes"),
    ("POST", "/api/edges"),
    ("PATCH", "/api/nodes/x"),
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

    A page anywhere on the web could submit ``<form action="http://127.0.0.1:8600
    /api/review/accept" method="post" enctype="text/plain">`` — a CORS-*simple*
    request, so no preflight, so the absence of CORS headers stopped the
    attacker reading the reply and nothing else. It returned 200, moved an
    agent's proposal to ``active``, and recorded the event as ``actor: human``:
    the log said a human reviewed agent output, and no human had.

    Two independent layers now refuse it: the content type is not
    ``application/json``, and the request is cross-site.
    """
    proposal = service.create_node(type="note", title="Agent draft", principal=agent(AGENT))

    response = client.post(
        "/api/review/accept",
        guard=False,
        headers={**CROSS_ORIGIN_HEADERS, "Content-Type": "text/plain;charset=UTF-8"},
        content=json.dumps({"ids": [proposal.id]}),
    )

    assert response.status_code == 403
    assert service.get_node(proposal.id, principal=owner()).state == "proposed"
    assert _events("node.accept") == []


def test_a_bodyless_cross_origin_post_cannot_archive_live_content(client, fresh_db):
    """``fetch(url, {method:'POST', mode:'no-cors'})`` — no body, no content type."""
    node = service.create_node(type="note", title="Live human content", principal=owner())

    response = client.post(
        f"/api/nodes/{node.id}/archive", guard=False, headers=CROSS_ORIGIN_HEADERS
    )

    assert response.status_code == 403
    assert service.get_node(node.id, principal=owner()).state == "active"


def test_a_bodyless_cross_origin_post_cannot_archive_an_edge(client, fresh_db):
    source = service.create_node(type="note", title="Source", principal=owner())
    destination = service.create_node(type="note", title="Destination", principal=owner())
    edge = service.create_edge(source.id, destination.id, "relates_to", principal=owner())

    response = client.post(
        f"/api/edges/{edge.id}/archive", guard=False, headers=CROSS_ORIGIN_HEADERS
    )

    assert response.status_code == 403
    active = service.list_edges(node_id=source.id, principal=owner())
    assert [item.id for item in active] == [edge.id]


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", ""])
def test_an_edge_archive_requires_json_content_type(client, fresh_db, content_type):
    source = service.create_node(type="note", title="Source", principal=owner())
    destination = service.create_node(type="note", title="Destination", principal=owner())
    edge = service.create_edge(source.id, destination.id, "relates_to", principal=owner())
    headers = {**CLIENT_HEADERS}
    if content_type:
        headers["Content-Type"] = content_type

    response = client.post(
        f"/api/edges/{edge.id}/archive", guard=False, headers=headers, content=b""
    )

    assert response.status_code == 415
    active = service.list_edges(node_id=source.id, principal=owner())
    assert [item.id for item in active] == [edge.id]


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
    assert created.json()["created_by"] == OWNER_ACTOR

    # `make web-dev` proxies from :5700 with the browser's own Host and Origin,
    # and ports are not compared, so the dev server keeps working.
    dev = client.post(
        "/api/nodes",
        guard=False,
        headers={
            "Host": "localhost:5700",
            "Origin": "http://localhost:5700",
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
    service.create_node(type="note", title="Private content", principal=owner())

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
    named = _session_client(
        http_api.create_app(allowed_hosts=http_api.resolve_allowed_hosts("0.0.0.0", ["nodum.lan"]))
    )
    assert named.get("/api/types", headers={"Host": "nodum.lan"}).status_code == 200
    assert named.get("/api/types", headers={"Host": "elsewhere.example"}).status_code == 400

    anywhere = _session_client(
        http_api.create_app(allowed_hosts=http_api.resolve_allowed_hosts("0.0.0.0", ["*"]))
    )
    assert anywhere.get("/api/types", headers={"Host": "elsewhere.example"}).status_code == 200


# ── 4c. Upload limits and the two routes' type policy ─────────────────────────


def test_an_oversized_upload_is_refused_before_it_is_buffered(fresh_db, tmp_path):
    """The cap has to bite in ``receive``, not in ``register_asset``.

    Before this, ``AssetTooLarge`` was the only limit and it fired after
    Starlette had spooled the whole part to disk *and* the handler had copied it
    to a second temp file: a 400 MB upload measured 839 MB of ``/tmp``, and
    tripping the real 1 GB blob limit needed more than 2 GB of it first.
    """
    limit = 64 * 1024
    client = _session_client(http_api.create_app(max_body_bytes=limit))
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
    assert assets.list_assets(principal=owner()) == []


def test_the_body_cap_covers_json_routes_too(fresh_db):
    """One ceiling on what this server will read, not one per route."""
    client = _session_client(http_api.create_app(max_body_bytes=4096))
    response = client.post("/api/nodes", json={"type": "note", "content": "x" * 8192})
    assert response.status_code == 413


def _put_through_a_grant(client, name: str, payload: bytes) -> httpx.Response:
    """Mint an upload grant and PUT ``payload`` at it, as an anonymous holder would.

    The grant declares ``application/octet-stream`` (``_mint_upload``'s default)
    for every call, which is the point: the declared MIME is a promise made
    before the bytes moved, and the policy reads the bytes instead.
    """
    grant = _mint_upload(client, name, len(payload))["grant"]
    return Client(client.app).put(grant["url"], guard=False, content=payload)


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
    """The type is decided by the bytes, never by the name the client chose.

    This route registers bytes and writes no describing node, so what describes
    them is the note that inlines a rendition of them — which is why its admitted
    set is the rasters and nothing else. The HTML row is refused *here* and
    admitted on the capability route, which ingests it: the policy is one rule
    parameterised by what the route can act on, not one rule per route.
    """
    response = client.post("/api/assets", files={"file": (name, payload)})

    assert response.status_code == 400, response.text
    assert response.json()["error"]["type"] == "UnsupportedRendition"
    # The refusal names what *is* accepted, not only what was refused.
    assert "image/png" in response.json()["error"]["message"]
    assert assets.list_assets(principal=owner()) == []


def test_a_document_is_refused_by_the_asset_route_and_ingested_by_the_capability_one(
    client, fresh_db
):
    """The split this project opened on, closed in both directions.

    A PDF is not something a note can inline and render, so ``/api/assets``
    refuses it and says what it is. It *is* something ingestion turns into a
    subgraph, so the capability route takes it — and the stored MIME follows the
    bytes, not the grant's declaration.
    """
    payload = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\n"

    refused = client.post("/api/assets", files={"file": ("paper.pdf", payload)})
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["type"] == "UnsupportedRendition"
    assert "application/pdf" in refused.json()["error"]["message"]
    assert assets.list_assets(principal=owner()) == []

    result = _ok(_put_through_a_grant(client, "paper.pdf", payload))

    assert result["asset"]["mime"] == "application/pdf"
    assert result["asset_ref"]["props"]["asset_hash"] == result["asset"]["hash"]


def _padded_zip() -> bytes:
    """A real zip behind 4 KiB of ASCII — still a zip, and text to a head-only sniff."""
    body = io.BytesIO()
    with zipfile.ZipFile(body, "w") as archive:
        archive.writestr("word/document.xml", "<w:document/>")
    return b"A" * 4096 + body.getvalue()


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        # The original case: `MZ` plus NULs matches no signature and fails the
        # NUL test, so it is not text either. `/api/assets` already refused it;
        # `PUT /api/uploads/{token}` used to register *and describe* it.
        ("a bare executable", b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00this is a program"),
        # Review F4's first bypass: any BOM used to mean text *before* the NUL
        # test ran, so three bytes in front of the same program was admitted,
        # stored as `text/plain`, and given a whole subgraph.
        ("a BOM in front of it", codecs.BOM_UTF8 + b"MZ\x90\x00\x03\x00\x00\x00\x00\x00"),
        ("a BOM in front of NULs", codecs.BOM_UTF8 + b"\x00" * 4096),
        # F4's second bypass: a head-only window sees only the padding, and a
        # zip's central directory is at the end — where the tail window looks.
        ("an ASCII-padded zip", _padded_zip()),
        # Review F5: `any()` over an empty window is False, so every test in the
        # heuristic passed vacuously and a zero-byte `.exe` was admitted.
        ("nothing at all", b""),
    ],
)
def test_a_binary_the_window_test_catches_is_refused_by_both_upload_routes(
    client, fresh_db, label, payload
):
    """What the type policy delivers on a renamed binary, stated exactly.

    The text decision is a **windowed heuristic**: it says neither end of the
    file looks binary in 4 KiB. It is not a guarantee that a renamed binary is
    refused — a NUL-free, control-character-free format still gets in as text —
    and each row here is a way past it that *was* open and is now closed.
    """
    inline = client.post("/api/assets", files={"file": ("innocent.png", payload)})
    assert inline.status_code == 400, (label, inline.text)
    assert inline.json()["error"]["type"] == "UnsupportedRendition"

    capability = _put_through_a_grant(client, "innocent.pdf", payload)
    assert capability.status_code == 400, (label, capability.text)
    assert capability.json()["error"]["type"] == "UnsupportedRendition"
    message = capability.json()["error"]["message"]
    # The widest network route names what it does accept, and — having no wider
    # route to redirect to — points at the CLI for the rest.
    assert "application/pdf" in message
    assert "nodum ingest file" in message

    assert assets.list_assets(principal=owner()) == []
    assert service.list_nodes(type="asset_ref", principal=owner()) == []


def test_a_pdf_whose_header_is_one_byte_in_still_ingests_as_a_pdf(client, fresh_db):
    """Review F3: a signature is definite evidence and a window test is not.

    `\\n` before `%PDF-` is read fine by both `pypdf` and `pypdfium2`, matches no
    signature, and sniffs as text — which used to overrule the filename's
    `application/pdf` and cost the document its handler, its `page:<n>` rasters,
    and put raw PDF bytes into `assets.extracted_text` and the FTS index.

    Note the fixture's reach: `sample.pdf` is uncompressed, so its bytes are
    NUL-free and it sniffs as *text*, which the name then outranks. A PDF with a
    compressed stream sniffs as nothing — covered by
    `test_a_pdf_with_binary_streams_and_a_displaced_header_is_admitted`, the case
    that was refused at the door while this test was green.
    """
    payload = b"\n" + PDF_FIXTURE.read_bytes()

    result = _ok(_put_through_a_grant(client, "shifted.pdf", payload))

    assert result["asset"]["mime"] == "application/pdf"
    if find_spec("pypdf") is not None:
        assert result["extraction"]["handler"] == "pdf"
        assert len(result["pages"]) == 2
        assert "%PDF" not in (result["asset"]["extracted_text"] or "")
    if find_spec("pypdfium2") is not None:
        raster = client.get(f"/api/assets/{result['asset']['hash']}/rendition/page:1")
        assert raster.status_code == 200, raster.text


def test_a_pdf_with_binary_streams_and_a_displaced_header_is_admitted(client, fresh_db, tmp_path):
    """The live end-to-end pass found this; the suite could not.

    Every PDF a human drops carries compressed streams, so it does not sniff as
    text — and with the header displaced it matched no leading signature either,
    so the route answered 400 *"not a type this API recognises"* for a document
    `pypdf` and PDFium both read. The sibling test above stayed green throughout,
    because its hand-assembled fixture is NUL-free and took the text branch.

    Drives the route rather than `ingest_file`, since admission is the thing
    under test and the pipeline has no admission policy.
    """
    real = tmp_path / "real.pdf"
    Image.new("RGB", (24, 18), "teal").save(real, "PDF")
    assert b"\x00" in real.read_bytes(), "fixture must carry binary stream bytes"
    payload = b"\n" + real.read_bytes()

    result = _ok(_put_through_a_grant(client, "scan.pdf", payload))

    assert result["asset"]["mime"] == "application/pdf"
    assert result["created"] is True
    if find_spec("pypdf") is not None:
        assert result["extraction"]["handler"] == "pdf"
    if find_spec("pypdfium2") is not None:
        raster = client.get(f"/api/assets/{result['asset']['hash']}/rendition/page:1")
        assert raster.status_code == 200, raster.text


def test_a_binary_that_is_not_a_pdf_is_still_refused_by_the_displaced_scan(client, fresh_db):
    """The scan widens what is admitted by exactly one format, not by "binary"."""
    payload = b"MZ\x90\x00\x03\x00" + b"\x00" * 300 + b"this is a program"

    refused = _put_through_a_grant(client, "innocent.pdf", payload)

    assert refused.status_code == 400, refused.text
    assert "not a type this API recognises" in refused.json()["error"]["message"]


@pytest.mark.parametrize(
    ("name", "options", "sniffed"),
    [
        ("photo.jp2", {}, "image/jp2"),
        ("photo.avif", {}, "image/avif"),
        ("photo.ico", {}, "image/x-icon"),
        ("scan.tif", {"big_tiff": True}, "image/tiff"),
    ],
)
def test_a_raster_this_build_renders_is_admitted_by_both_routes(
    client, fresh_db, tmp_path, name, options, sniffed
):
    """Review F8: the admitted set has to be what this install can act on.

    All four carry NULs in their header, so the text heuristic could never name
    them and both routes 400'd — while `register_asset` + a `thumb` of the very
    same bytes succeeded. Base took them on the capability route.
    """
    source = tmp_path / name
    Image.new("RGB", (64, 48), "red").save(source, **options)
    payload = source.read_bytes()
    assert assets.sniff_mime(source) == sniffed, "admission is decided on this"

    registered = _ok(client.post("/api/assets", files={"file": (name, payload)}))
    # The name keeps its specificity inside the family, so `.ico` stores the
    # `mimetypes` spelling rather than the sniffer's; both are rasters.
    assert registered["mime"].startswith("image/")
    thumb = client.get(f"/api/assets/{registered['hash']}/rendition/thumb")
    assert thumb.status_code == 200, thumb.text

    ingested = _ok(_put_through_a_grant(client, name, payload))
    assert ingested["asset"]["hash"] == registered["hash"]
    assert ingested["asset_ref"]["props"]["asset_hash"] == registered["hash"]


def test_a_document_no_handler_claims_is_refused_over_http_but_not_by_the_pipeline(
    client, fresh_db, tmp_path
):
    """The deliberate cost of the policy, and its boundary.

    A ``.docx`` is a zip: no signature this system reads, NULs in the first
    window, no handler in :mod:`nodum.extract`. Over the network we take from a
    stranger, so both routes refuse it. The pipeline's own tolerance — a file no
    handler claims is still registered and described — is unchanged, and that is
    what ``nodum ingest file`` gives an operator registering a file they own.
    """
    payload = b"PK\x03\x04\x14\x00\x06\x00\x08\x00\x00\x00word/document.xml"

    assert client.post("/api/assets", files={"file": ("notes.docx", payload)}).status_code == 400
    assert _put_through_a_grant(client, "notes.docx", payload).status_code == 400
    assert assets.list_assets(principal=owner()) == []

    source = tmp_path / "notes.docx"
    source.write_bytes(payload)
    ingested = ingest.ingest_file(source, principal=owner())

    assert ingested.created is True
    assert ingested.extraction.handler == "none"
    assert ingested.asset_ref.props["asset_hash"] == ingested.asset.hash


def test_a_decompression_bomb_is_refused_on_the_capability_route_too(
    client, fresh_db, tmp_path, monkeypatch
):
    """Same bomb, same header read; the budget was simply never run on this route.

    It applies wherever the bytes turn out to be an image — a type with no pixels
    to count (a PDF, an audio file, text) skips it rather than being refused.
    What this route uses is the **bomb** half of the budget: Pillow's own
    threshold, lowered here rather than met, since meeting it means allocating
    179 megapixels.
    """
    bomb = tmp_path / "bomb.png"
    Image.new("L", (8000, 8000)).save(bomb, "PNG")
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)

    response = _put_through_a_grant(client, "bomb.png", bomb.read_bytes())

    assert response.status_code == 400, response.text
    assert response.json()["error"]["type"] == "ImageTooLarge"
    assert assets.list_assets(principal=owner()) == []
    assert service.list_nodes(type="asset_ref", principal=owner()) == []


def test_a_big_scan_is_ingested_and_refused_by_the_rendition_route(client, fresh_db, tmp_path):
    """Capability must not gate admission (review F9).

    A 49 MP PNG is over the 40 MP *rendition* ceiling and is an ordinary
    document — a 600 dpi A3 scan is ~70 MP. Making that ceiling an admission
    rule on the ingestion route contradicted this change's own principle, the
    one that keeps a PDF admissible on an install with no `pdf` extra. So the
    ceiling stays where a rendition is the point: `POST /api/assets`.
    """
    scan = tmp_path / "scan.png"
    Image.new("L", (7000, 7000)).save(scan, "PNG")
    payload = scan.read_bytes()

    ingested = _ok(_put_through_a_grant(client, "scan.png", payload))
    assert ingested["created"] is True
    assert ingested["asset"]["mime"] == "image/png"
    stored = [row.hash for row in assets.list_assets(principal=owner())]
    assert stored == [ingested["asset"]["hash"]]

    # The ceiling still holds where it means something: a rendition of those
    # bytes, and the route whose entire purpose is producing one.
    rendition = client.get(f"/api/assets/{ingested['asset']['hash']}/rendition/thumb")
    assert rendition.status_code == 400
    assert rendition.json()["error"]["type"] == "ImageTooLarge"
    refused = client.post("/api/assets", files={"file": ("scan.png", payload)})
    assert refused.status_code == 400
    assert refused.json()["error"]["type"] == "ImageTooLarge"


def test_an_unreadable_image_is_a_400_on_the_anonymous_route_and_names_no_path(client, fresh_db):
    """Review F1 and F6 in one request, on the route with no session behind it.

    Both payloads match an image signature, so Pillow's plugin `accept()`s them
    and the parse then fails with a **bare** `OSError` —
    `EXCEPTION_STATUS[OSError]` is 500, and the token was already spent by it.
    And the message may not carry the spool path: this caller is a stranger.
    """
    for name, payload in (
        ("x.bmp", b"BM" + b"not really a bitmap, no NULs here"),
        ("x.webp", b"RIFF\x1a\x00\x00\x00WEBPVP8L" + b"\x00" * 8),
    ):
        response = _put_through_a_grant(client, name, payload)

        assert response.status_code == 400, response.text
        assert response.json()["error"]["type"] == "UnsupportedRendition"
        message = response.json()["error"]["message"]
        assert name in message
        assert "/tmp" not in message and "nodum-upload-" not in message
    assert assets.list_assets(principal=owner()) == []


@pytest.mark.skipif(
    find_spec("pypdfium2") is None or find_spec("pypdf") is None,
    reason="the pdf extra is not installed",
)
def test_an_admitted_pdf_through_the_capability_route_becomes_the_whole_subgraph(client, fresh_db):
    """The route's exit criterion: an admitted document lands as knowledge.

    Not only the bytes — the describing node, the source carrying the extracted
    text, the provenance edge, one block per page, and a ``page:<n>`` raster that
    resolves, which is exactly what the stored MIME being ``application/pdf``
    buys (the grant said ``application/octet-stream``).
    """
    result = _ok(_put_through_a_grant(client, "ostrogoths.pdf", PDF_FIXTURE.read_bytes()))

    assert result["created"] is True
    assert result["asset"]["mime"] == "application/pdf"
    assert result["extraction"]["handler"] == "pdf"
    assert result["asset_ref"]["props"]["asset_hash"] == result["asset"]["hash"]
    assert result["edges"][0]["type"] == "derived_from"
    assert [page["props"]["page"] for page in result["pages"]] == [1, 2]
    assert result["asset_ref"]["created_by"] == OWNER_ACTOR

    raster = client.get(f"/api/assets/{result['asset']['hash']}/rendition/page:1")
    assert raster.status_code == 200, raster.text
    assert Image.open(io.BytesIO(raster.content)).format == "WEBP"


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


# ── 4e. Ingestion and the two capability URLs ────────────────────────────────
#
# `POST /api/ingest` is an ordinary session route. The two token routes are
# not: they answer with no session, no origin proof and no content type,
# because the single-use token in the path is the whole credential. What must
# still hold for them is everything that is *not* about ambient credentials —
# the Host check, the body ceiling, single use, expiry, and refusals that read
# identically whichever of the four failures happened.


class _FixtureHandler(BaseHTTPRequestHandler):
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
    """A loopback HTTP server serving one canned body — the suite never leaves the machine.

    Same shape as ``tests/test_ingest.py``'s: ``POST /api/ingest`` with a
    ``url`` makes the *server* fetch, so the one thing this must never be is
    the real network.
    """
    server = HTTPServer(("127.0.0.1", 0), _FixtureHandler)
    server.canned = (b"", "text/plain")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _fixture_url(server, path: str) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}{path}"


def _expire(token: str) -> None:
    """Backdate one capability token's expiry — the clock is never slept on."""
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE url_tokens SET expires_at = datetime('now', '-1 day') WHERE token_hash = ?",
            (hashlib.sha256(token.encode()).hexdigest(),),
        )
        conn.commit()
    finally:
        conn.close()


def _ingest_file(client, tmp_path, payload: bytes, name: str = "report.pdf") -> dict:
    """Ingest one local file over HTTP and return the envelope."""
    source = tmp_path / name
    source.write_bytes(payload)
    return _ok(client.post("/api/ingest", json={"path": str(source)}))


def _mint_download(client, id_or_hash: str) -> dict:
    """Mint a download URL over HTTP; assert it points at this server's own route."""
    grant = _ok(client.post(f"/api/assets/{id_or_hash}/download-url"))
    assert grant["url"].startswith(f"{BASE_URL}{urls.TOKEN_PATHS['download']}/")
    assert grant["token"] in grant["url"]
    return grant


def _mint_upload(client, name: str, size: int, **extra) -> dict:
    """Mint an upload grant over HTTP and return the envelope."""
    body = {"name": name, "mime": "application/octet-stream", "size": size, **extra}
    return _ok(client.post("/api/uploads", json=body))


def test_ingest_registers_extracts_and_describes_a_local_file(client, fresh_db, tmp_path):
    """The pipeline's own guarantees, reached through the API rather than the CLI."""
    source = tmp_path / "hydrology.txt"
    source.write_text("Vercingetorix basin hydrology", encoding="utf-8")

    payload = _ok(client.post("/api/ingest", json={"path": str(source), "title": "Basin"}))

    assert payload["created"] is True
    assert payload["extraction"]["handler"] == "text"
    assert payload["source"]["title"] == "Basin"
    assert payload["source"]["content"] == "Vercingetorix basin hydrology"
    assert payload["asset_ref"]["props"]["asset_hash"] == payload["asset"]["hash"]
    assert payload["edges"][0]["type"] == "derived_from"
    # A human write lands live, and is attributed to the session's human.
    assert payload["source"]["state"] == "active"
    assert payload["source"]["created_by"] == OWNER_ACTOR
    assert _events("asset.ingest")[0].actor == OWNER_ACTOR

    # Idempotent per (hash, space): a re-run finds the subgraph it wrote.
    again = _ok(client.post("/api/ingest", json={"path": str(source)}))
    assert again["created"] is False
    assert again["asset_ref"]["id"] == payload["asset_ref"]["id"]


def test_ingest_fetches_a_url_from_a_loopback_server(client, fresh_db, fixture_server):
    """`url` makes the server fetch — asserted against a fixture, never the network."""
    fixture_server.canned = (
        b"<html><body><p>Basin hydrology</p></body></html>",
        "text/html; charset=utf-8",
    )

    payload = _ok(
        client.post("/api/ingest", json={"url": _fixture_url(fixture_server, "/article")})
    )

    assert payload["extraction"]["handler"] == "html"
    assert payload["source"]["content"] == "Basin hydrology"
    assert payload["asset_ref"]["props"]["url"].endswith("/article")
    assert payload["source"]["created_by"] == OWNER_ACTOR


def test_ingest_takes_exactly_one_of_path_and_url(client, fresh_db, tmp_path):
    """Both or neither is a 400: a precedence rule between them is a coin flip."""
    source = tmp_path / "note.txt"
    source.write_text("either", encoding="utf-8")

    for body in ({}, {"path": str(source), "url": "http://127.0.0.1:1/x"}):
        response = client.post("/api/ingest", json=body)
        assert response.status_code == 400, response.text
        assert "exactly one" in response.json()["error"]["message"]

    # And a field of the wrong type is a 400 too, not a traceback further down.
    for body in ({"path": 12}, {"url": ["http://x"]}, {"path": str(source), "name": 7}):
        response = client.post("/api/ingest", json=body)
        assert response.status_code == 400, response.text
        assert "Traceback" not in response.text

    assert service.list_nodes(type="source", principal=owner()) == []


def test_a_smuggled_identity_on_an_ingest_is_ignored_not_honored(client, fresh_db, tmp_path):
    """The smuggling pattern of §3, applied to the surface's newest write."""
    source = tmp_path / "note.txt"
    source.write_text("smuggled", encoding="utf-8")

    payload = _ok(
        client.post(
            f"/api/ingest?actor={AGENT}",
            json={"path": str(source), "actor": AGENT, "principal": AGENT, "created_by": AGENT},
            headers={"X-Actor": AGENT},
        )
    )

    assert payload["source"]["created_by"] == OWNER_ACTOR
    assert payload["asset_ref"]["created_by"] == OWNER_ACTOR
    assert payload["source"]["state"] == "active"  # an agent write would land proposed
    assert [event.actor for event in _events("asset.ingest")] == [OWNER_ACTOR]


def test_a_download_url_serves_the_exact_original_bytes_once(client, fresh_db, tmp_path):
    """The escape hatch's whole job — and it is spent by the first redemption."""
    original = b"%PDF-1.4\nexactly these bytes\n"
    ingested = _ingest_file(client, tmp_path, original)
    grant = _mint_download(client, ingested["asset"]["hash"])

    response = client.get(grant["url"])

    assert response.status_code == 200
    assert response.content == original
    assert response.headers["content-length"] == str(len(original))

    second = client.get(grant["url"])
    assert second.status_code == 400
    assert second.json()["error"]["type"] == "TokenInvalid"


def test_a_downloaded_original_is_never_served_under_its_own_mime(client, fresh_db, tmp_path):
    """Serving a stranger's MIME back is how a file host becomes stored XSS.

    The bytes below are a script in this origin — the same origin that may
    write to this API — and the page CSP is on the static route, not here. So
    the response says octet-stream, says ``nosniff`` so the browser cannot
    overrule it, and says ``attachment`` so it is saved rather than rendered.
    """
    ingested = _ingest_file(client, tmp_path, b"<script>alert(1)</script>", name="page.html")
    assert ingested["asset"]["mime"] == "text/html"  # what the store recorded
    grant = _mint_download(client, ingested["asset"]["hash"])

    response = client.get(grant["url"])

    assert response.status_code == 200
    assert response.headers["content-type"] == http_api.DOWNLOAD_CONTENT_TYPE
    assert "html" not in response.headers["content-type"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment; ")
    # The one name attached to these bytes that no stranger chose.
    assert ingested["asset"]["hash"] in disposition
    assert "page.html" not in disposition


def test_a_download_url_needs_no_session_but_still_needs_the_right_host(client, fresh_db, tmp_path):
    """Both halves of the exemption, in one test.

    No session at all is the point of the hatch: the holder of the URL is an
    agent host with no account here. The ``Host`` check is *not* part of the
    exemption — it is about which server the request reached, which a
    capability changes nothing about — and it refuses before the handler, so
    the token survives to be spent by its rightful holder.
    """
    original = b"no cookie required"
    ingested = _ingest_file(client, tmp_path, original, name="notes.txt")
    grant = _mint_download(client, ingested["asset"]["hash"])
    anonymous = Client(client.app)  # no session cookie whatsoever
    assert anonymous.get("/api/assets").status_code == 401

    assert anonymous.get(grant["url"]).content == original

    rebound = _mint_download(client, ingested["asset"]["hash"])
    refused = anonymous.get(rebound["url"], headers={"Host": "attacker-rebind.example"})
    assert refused.status_code == 400
    assert refused.json()["error"]["type"] == "UntrustedHost"
    assert original not in refused.content
    # Refused by the guard, so the token was never spent.
    assert anonymous.get(rebound["url"]).content == original


def test_every_download_refusal_reads_identically(client, fresh_db, tmp_path):
    """Spent, expired, unknown, malformed, wrong kind — one status, one message.

    "Expired" would say a token once existed and "wrong kind" would say which
    route to try next; both are free intelligence for whoever is guessing.
    """
    ingested = _ingest_file(client, tmp_path, b"secret bytes")
    spent = _mint_download(client, ingested["asset"]["hash"])
    assert client.get(spent["url"]).status_code == 200
    expired = _mint_download(client, ingested["asset"]["hash"])
    _expire(expired["token"])
    upload = _mint_upload(client, "scan.bin", 4)["grant"]
    download_path = urls.TOKEN_PATHS["download"]

    refusals = [
        client.get(spent["url"]),
        client.get(expired["url"]),
        client.get(f"{download_path}/{auth.TOKEN_PREFIX}{'a' * 43}"),
        client.get(f"{download_path}/not-a-token-at-all"),
        # An upload grant presented at the download route.
        client.get(f"{download_path}/{upload['token']}"),
    ]

    assert {response.status_code for response in refusals} == {400}
    assert len({response.text for response in refusals}) == 1
    assert urls.INVALID_TOKEN_MESSAGE in refusals[0].text
    assert b"secret bytes" not in refusals[0].content
    # The stray request at the wrong route did not burn the upload grant.
    assert urls.consume(upload["token"], kind="upload")["kind"] == "upload"


def test_a_head_probe_does_not_spend_a_download_token(client, fresh_db, tmp_path):
    """HEAD must never redeem the capability: a probe is not a download.

    Starlette answers HEAD on a GET route by running the handler with the
    body suppressed, so a bare route would *spend the single-use token* on a
    request that asked for no bytes — and the real GET behind it would come
    back refused. The route refuses HEAD with 405 (``Allow: GET``), the probe
    is not event-logged, and the token survives for the GET that wants it.
    """
    original = b"%PDF-1.4\nprobe me not\n"
    ingested = _ingest_file(client, tmp_path, original)
    grant = _mint_download(client, ingested["asset"]["hash"])
    assert _events("asset.download") == []

    probe = client.request("HEAD", grant["url"])

    assert probe.status_code == 405
    assert set(probe.headers["allow"].split(", ")) == {"GET"}
    assert probe.content == b""  # HEAD carries no body — the status is the refusal
    assert _events("asset.download") == []  # the probe spent nothing

    # The same refusal through a body-bearing method, so the error is readable:
    # the app's own 405 machinery (``MethodNotAllowed``), not an accidental one.
    delete = client.delete(grant["url"])
    assert delete.status_code == 405
    assert delete.json()["error"]["type"] == "MethodNotAllowed"
    assert _events("asset.download") == []

    response = client.get(grant["url"])

    assert response.status_code == 200
    assert response.content == original
    assert len(_events("asset.download")) == 1  # spent by the GET, exactly once


def test_the_download_response_closes_the_database_before_streaming(
    client, fresh_db, tmp_path, monkeypatch
):
    """M15: the client-paced stream holds no database handle, WAL pin included.

    The blob copy finishes inside :func:`_original_response`, so the
    connection it used is closed before a single chunk streams — and with the
    connection, the blob's open read transaction and its WAL snapshot, which a
    stalled client would otherwise pin for the whole (potentially gigabyte,
    client-paced) transfer.
    """
    original = b"%PDF-1.4\nspooled, never pinned\n"
    ingested = _ingest_file(client, tmp_path, original)
    real_connect = db.connect
    seen: list[sqlite3.Connection] = []

    def tracking_connect(path=None):
        conn = real_connect(path)
        seen.append(conn)
        return conn

    monkeypatch.setattr(http_api.db, "connect", tracking_connect)
    response = http_api._original_response(ingested["asset"]["hash"], fresh_db)

    assert len(seen) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        seen[0].execute("SELECT 1")  # closed before the stream even started

    async def drain() -> bytes:
        return b"".join([chunk async for chunk in response.body_iterator])

    assert asyncio.run(drain()) == original


def test_the_download_spool_is_unlinked_once_the_stream_is_served(
    client, fresh_db, tmp_path, monkeypatch
):
    """M15: the spooled temp file dies with the response, not with the handler.

    The temp file is the one artifact a download leaves behind, so it must be
    gone once the response is done — the alternative is a gigabyte-scale temp
    file per download accumulating until a reboot.
    """
    original = b"%PDF-1.4\nspool lifecycle\n"
    ingested = _ingest_file(client, tmp_path, original)
    real_tnf = http_api.tempfile.NamedTemporaryFile
    spooled: list[str] = []

    def tracking_tnf(*args, **kwargs):
        handle = real_tnf(*args, **kwargs)
        spooled.append(handle.name)
        return handle

    monkeypatch.setattr(http_api.tempfile, "NamedTemporaryFile", tracking_tnf)
    grant = _mint_download(client, ingested["asset"]["hash"])

    response = client.get(grant["url"])

    assert response.status_code == 200
    assert response.content == original
    assert len(spooled) == 1
    assert not Path(spooled[0]).exists(), "the spool must be unlinked after the stream"


def test_closing_the_download_stream_mid_transfer_unlinks_the_spool(tmp_path):
    """M15: a client hang-up closes the generator; the finally must still unlink.

    Starlette closes a streaming response's generator instead of draining it
    when the client disconnects, so the unlink lives in the generator's
    ``finally`` — this drives exactly that: consume one chunk, close, and the
    file is gone.
    """
    spool = tmp_path / "spool.tmp"
    spool.write_bytes(b"z" * (2 * http_api.UPLOAD_CHUNK_BYTES + 7))

    async def drive() -> None:
        chunks = http_api._spooled_chunks(spool)
        first = await chunks.__anext__()
        assert len(first) == http_api.UPLOAD_CHUNK_BYTES
        await chunks.aclose()

    asyncio.run(drive())

    assert not spool.exists()


def test_an_upload_url_ingests_the_bytes_the_grant_was_minted_for(client, fresh_db):
    """A PUT with no cookie, no origin headers and no content type at all.

    Design §5.7 rule 4 ends "normal ingestion runs after the PUT", so the
    response is the whole ingestion, not a bare asset: without that the hatch
    dead-ends, because no surface can turn a stored hash into a subgraph.
    """
    payload = b"scanned page bytes"
    # Minted by a **second** human, not the owner session: the redemption
    # presents no identity of its own, so its attribution comes from the token
    # row's `created_by`. Asserting the owner's own actor three times over
    # could not tell a handler that hardcoded `owner_principal()` from one
    # that re-mints the grant's authoriser (review MAJOR-4).
    second = service.create_human("second", principal=owner())
    second_actor = f"human:{second.id}"
    grant = urls.mint_upload(
        "scan.txt", "text/plain", len(payload), principal=auth.principal_from_actor(second_actor)
    ).grant
    assert grant is not None, "a mint without sha256 always grants a URL"
    assert grant.url.startswith(f"{BASE_URL}{urls.TOKEN_PATHS['upload']}/")
    assert grant.max_bytes == len(payload)
    anonymous = Client(client.app)

    result = _ok(anonymous.put(grant.url, guard=False, content=payload))

    assert result["asset"]["hash"] == hashlib.sha256(payload).hexdigest()
    assert result["asset"]["size_bytes"] == len(payload)
    assert result["asset"]["original_name"] == "scan.txt"
    assert [row.hash for row in assets.list_assets(principal=owner())] == [result["asset"]["hash"]]
    # The bytes are described, so they are reachable — the whole point.
    assert result["asset_ref"]["props"]["asset_hash"] == result["asset"]["hash"]
    assert result["source"]["content"] == "scanned page bytes"
    assert result["created"] is True
    # Everything is attributed to the principal who minted the grant — stored
    # state, since the request itself carries no identity. The second human is
    # named and the owner is ruled out, so a handler that minted under the
    # owner would fail here rather than pass in triplicate.
    assert result["asset_ref"]["created_by"] == second_actor
    assert result["asset_ref"]["created_by"] != OWNER_ACTOR
    assert [event.actor for event in _events("asset.upload")] == [second_actor]
    # Single use, exactly like the download side.
    assert anonymous.put(grant.url, guard=False, content=payload).status_code == 400


def test_an_upload_grant_dies_with_the_account_that_minted_it(client, fresh_db):
    """The principal is re-minted from the token row, so revocation reaches a
    capability already handed out — it must not outlive the account behind it.

    The status is asserted exactly (review F12): `auth.PrincipalDisabled` derives
    from `OSError` through `PermissionError`, so it landed on the `OSError → 500`
    row and was rewritten as `storage error: PrincipalDisabled` — a sentence the
    browser now shows a human for what is plainly a refusal. `>= 400` could not
    see that.
    """
    payload = b"late delivery"
    agent_principal = agent("courier", grants={"meta": "read", "main": "suggest"})
    grant = urls.mint_upload(
        "drop.txt", "text/plain", len(payload), principal=agent_principal
    ).grant
    service.disable_agent("courier", principal=owner())

    response = Client(client.app).put(grant.url, guard=False, content=payload)

    assert response.status_code == 403, response.text
    assert response.json()["error"]["type"] == "PrincipalDisabled"
    assert "storage error" not in response.text
    assert service.list_nodes(type="source", principal=owner()) == []


def test_a_grant_revoked_before_redemption_stores_no_bytes(client, fresh_db):
    """Review B6: a write grant revoked between mint and redeem must not leave bytes.

    ``mint_upload`` probes the grant before the transfer, but the gap is the
    window *after* the mint: a grant revoked there used to store the body
    anyway, because the refusal arrived from the node write, after
    ``register_asset`` had committed the bytes. The redemption re-mints the
    grant's principal and ``ingest_upload`` runs the same pre-registration
    probe, so the refused PUT answers 403 with ``asset_blobs`` still empty.
    """
    payload = b"revoked before redemption"
    courier = agent("courier", grants={"meta": "read", "main": "suggest"})
    grant = urls.mint_upload("drop.txt", "text/plain", len(payload), principal=courier).grant
    # The write grant is gone by redemption time, while the read grant that
    # keeps the space resolvable stays — the exact case the old code refused
    # only after storing the bytes.
    service.grant("courier", "main", "read", principal=owner())

    response = Client(client.app).put(grant.url, guard=False, content=payload)

    assert response.status_code == 403, response.text
    assert response.json()["error"]["type"] == "GrantNotPermitted"
    assert assets.list_assets(principal=owner()) == []
    conn = db.connect(fresh_db)
    try:
        assert conn.execute("SELECT count(*) AS n FROM asset_blobs").fetchone()["n"] == 0
    finally:
        conn.close()


def test_a_grant_for_a_space_archived_since_the_mint_stores_no_bytes(client, fresh_db):
    """Review F13: a doomed upload used to store up to 32 MiB anyway.

    Registration ran *before* the space was resolved, so a grant minted against
    a space archived inside its five-minute TTL left bytes with no describing
    node, no FTS row and no delete route — while the client was told the upload
    failed. "Nothing was uploaded" has to be true, not merely reworded.
    """
    space = _ok(client.post("/api/spaces", json={"name": "shortlived"}))["id"]
    payload = b"filed into a space that is about to retire"
    grant = _mint_upload(client, "note.txt", len(payload), space=space)["grant"]
    _ok(client.post(f"/api/spaces/{space}/archive"))

    response = Client(client.app).put(grant["url"], guard=False, content=payload)

    assert response.status_code == 404, response.text
    assert assets.list_assets(principal=owner()) == []
    conn = db.connect(fresh_db)
    try:
        assert conn.execute("SELECT count(*) AS n FROM asset_blobs").fetchone()["n"] == 0
    finally:
        conn.close()


def test_an_upload_over_the_grants_ceiling_is_refused_while_it_streams(client, fresh_db):
    """The grant promised a size; the body is capped at it, not after it.

    ``MAX_REQUEST_BYTES`` is the outer ceiling and it is 32 MiB — far above
    this body. The cap being tested is the inner one the grant itself carries,
    which is what stops a 4-byte grant costing 32 MiB of ``/tmp`` to refuse.
    """
    grant = _mint_upload(client, "small.bin", 8)["grant"]
    anonymous = Client(client.app)

    response = anonymous.put(grant["url"], guard=False, content=b"x" * 4096)

    assert response.status_code == 413
    assert response.json()["error"]["type"] == "PayloadTooLarge"
    assert "8 bytes" in response.json()["error"]["message"]
    assert assets.list_assets(principal=owner()) == []


def test_a_capability_url_is_the_one_write_a_cross_origin_page_may_reach(client, fresh_db):
    """The exemption, stated positively — and its boundary, one slash away.

    A page on another origin that somehow holds a capability URL may spend it:
    the token is the authorisation, and a holder could have used ``curl``
    anyway. It may not reach the route that *hands them out*, which is an
    ordinary session write and keeps every gate.
    """
    payload = b"cross-origin bytes"
    grant = _mint_upload(client, "scan.bin", len(payload))["grant"]

    spent = Client(client.app).put(
        grant["url"], guard=False, headers=CROSS_ORIGIN_HEADERS, content=payload
    )
    assert spent.status_code == 200, spent.text

    refused = client.post(
        "/api/uploads",
        guard=False,
        headers={**CROSS_ORIGIN_HEADERS, "Content-Type": "application/json"},
        json={"name": "scan.bin", "mime": "application/octet-stream", "size": 4},
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["type"] == "CrossOriginRequest"


def test_upload_original_does_not_ingest_when_disconnected(client, fresh_db):
    """A client that hangs up mid-upload spends the token and changes nothing.

    The token is spent by the attempt — ``urls.consume`` runs first, by design
    — but ingestion is the step that writes, and it must not run for a client
    nobody is listening to: the bytes would become an asset and a subgraph
    while the one party that could read the outcome is gone, and the retry
    (which has to re-mint anyway) would find its document already described.

    The disconnect is driven for real, not monkeypatched: the app is called
    the way the ASGI server calls it, with a receive channel whose last
    message is ``http.disconnect`` — exactly what Starlette's
    ``Request.is_disconnected`` reads. Under the ``httpx.ASGITransport`` the
    rest of this file drives, the disconnect is not expressible: its
    ``receive`` only answers with body chunks and then waits for the response,
    so this one test drives the app directly.
    """
    payload = b"Vercingetorix basin hydrology"
    grant = _mint_upload(client, "hydrology.txt", len(payload))["grant"]

    sent: list[dict] = []
    messages = iter(
        [
            {"type": "http.request", "body": payload, "more_body": True},
            {"type": "http.request", "body": b"", "more_body": False},
            {"type": "http.disconnect"},
        ]
    )

    async def receive() -> dict:
        return next(messages)

    async def send(message: dict) -> None:
        sent.append(message)

    async def run() -> None:
        await client.app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "PUT",
                "scheme": "http",
                "path": f"/api/uploads/{grant['token']}",
                "raw_path": f"/api/uploads/{grant['token']}".encode(),
                "query_string": b"",
                "headers": [
                    (b"host", b"127.0.0.1:8600"),
                    (b"content-length", str(len(payload)).encode()),
                ],
                "server": ("127.0.0.1", 8600),
                "client": ("127.0.0.1", 12345),
                "root_path": "",
            },
            receive,
            send,
        )

    asyncio.run(run())

    status = next(message["status"] for message in sent if message["type"] == "http.response.start")
    assert status == 499, sent
    # The token is spent by the attempt: putting the same URL again is refused.
    again = Client(client.app).put(grant["url"], guard=False, content=payload)
    assert again.status_code == 400, again.text
    assert again.json()["error"]["type"] == "TokenInvalid"
    # And nothing was ingested: no asset, no describing node, no edges.
    assert assets.list_assets(principal=owner()) == []
    assert service.list_nodes(type="source", principal=owner()) == []
    assert service.list_edges(principal=owner()) == []


def test_upload_original_does_not_ingest_when_disconnected_before_the_pipeline(client, fresh_db):
    """The second disconnect check is pinned: a client that vanishes during
    the type sniff spends the token and changes nothing.

    The route checks :meth:`Request.is_disconnected` twice — once the body
    stream has finished, and once more before
    :func:`nodum.ingest.ingest_upload`, because the type policy between them
    reads and analyses the file and a client can vanish during that window.
    The test above delivers the disconnect as the third receive message, so
    the *first* check consumes it and never proves the second one exists.
    This variant delivers it as the fourth: the third message stands for the
    channel when the first check runs (a client still there — Starlette's
    ``is_disconnected`` discards any non-disconnect message), and the
    disconnect arrives only after the sniff, so the second check is the one
    that has to catch it. Removing that check leaves this test red: the
    ingest would run for nobody and the graph would be written.
    """
    payload = b"Vercingetorix basin hydrology"
    grant = _mint_upload(client, "hydrology.txt", len(payload))["grant"]

    sent: list[dict] = []
    messages = iter(
        [
            {"type": "http.request", "body": payload, "more_body": True},
            {"type": "http.request", "body": b"", "more_body": False},
            # What the first check sees: a client still talking, not a
            # disconnect. Consumed and discarded by ``is_disconnected``.
            {"type": "http.request", "body": b"", "more_body": False},
            {"type": "http.disconnect"},
        ]
    )

    async def receive() -> dict:
        return next(messages)

    async def send(message: dict) -> None:
        sent.append(message)

    async def run() -> None:
        await client.app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "PUT",
                "scheme": "http",
                "path": f"/api/uploads/{grant['token']}",
                "raw_path": f"/api/uploads/{grant['token']}".encode(),
                "query_string": b"",
                "headers": [
                    (b"host", b"127.0.0.1:8600"),
                    (b"content-length", str(len(payload)).encode()),
                ],
                "server": ("127.0.0.1", 8600),
                "client": ("127.0.0.1", 12345),
                "root_path": "",
            },
            receive,
            send,
        )

    asyncio.run(run())

    status = next(message["status"] for message in sent if message["type"] == "http.response.start")
    assert status == 499, sent
    # The token is spent by the attempt: putting the same URL again is refused.
    again = Client(client.app).put(grant["url"], guard=False, content=payload)
    assert again.status_code == 400, again.text
    assert again.json()["error"]["type"] == "TokenInvalid"
    # And nothing was ingested: no asset, no describing node, no edges.
    assert assets.list_assets(principal=owner()) == []
    assert service.list_nodes(type="source", principal=owner()) == []
    assert service.list_edges(principal=owner()) == []


def test_an_upload_request_that_declares_nonsense_is_a_400_not_a_500(client, fresh_db):
    """The body's three required fields, and the two optional ones, are typed.

    A JSON string where a byte count belongs used to reach a comparison in the
    service and a bind in SQLite; both are 500s for what is plainly the
    caller's mistake.
    """
    for body in (
        {},
        {"name": "x.bin"},
        {"name": "x.bin", "mime": "application/octet-stream"},
        {"name": "x.bin", "mime": "application/octet-stream", "size": "big"},
        {"name": "x.bin", "mime": "application/octet-stream", "size": True},
        {"name": 7, "mime": "application/octet-stream", "size": 4},
        {"name": "x.bin", "mime": "application/octet-stream", "size": 4, "sha256": 12},
        {"name": "x.bin", "mime": "application/octet-stream", "size": 4, "space": ["main"]},
        {"name": "x.bin", "mime": "application/octet-stream", "size": -1},
    ):
        response = client.post("/api/uploads", json=body)
        assert response.status_code == 400, (body, response.text)
        assert "Traceback" not in response.text


def test_an_upload_declared_over_the_ceiling_is_a_413_not_a_bare_value_error(client, fresh_db):
    """Review F11: a declared size above `urls.MAX_UPLOAD_BYTES` is the 413 case.

    It answered 400 with `ValueError` as its type, so the browser rendered
    `ValueError: size must be between 0 and 33554432 bytes, got …` at a human —
    while `PayloadTooLarge` sat right there, mapped, describing exactly this.
    """
    body = {"name": "x.bin", "mime": "application/octet-stream", "size": urls.MAX_UPLOAD_BYTES + 1}

    response = client.post("/api/uploads", json=body)

    assert response.status_code == 413, response.text
    assert response.json()["error"]["type"] == "PayloadTooLarge"
    assert "ValueError" not in response.text


def test_a_declared_hash_this_file_already_holds_moves_no_bytes(client, fresh_db, tmp_path):
    """Design §5.7 rule 4's dedup shortcut, over HTTP: an asset, and no grant."""
    ingested = _ingest_file(client, tmp_path, b"already here", name="known.bin")

    result = _mint_upload(client, "known.bin", 12, sha256=ingested["asset"]["hash"])

    assert result["grant"] is None
    assert result["asset"]["hash"] == ingested["asset"]["hash"]


def test_a_download_url_cannot_be_minted_for_an_asset_the_session_cannot_reach(client, fresh_db):
    """Minting is a scoped read, so a token can never widen anyone's reach."""
    response = client.post("/api/assets/deadbeef/download-url")

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "AssetNotFound"


def test_the_only_api_routes_outside_the_session_gate_are_login_and_the_capability_urls(fresh_db):
    """The exemption list, read off the live route table rather than trusted.

    ``LOGIN_PATH`` used to be the only exemption and was compared inline; the
    predicate is now the one place that decides, and this is what keeps the
    set it answers for from quietly growing.
    """
    app = http_api.create_app()
    open_paths = {
        route.path
        for route in app.routes
        if route.path.startswith("/api") and not http_api._needs_a_session(route.path)
    }

    assert open_paths == {"/api/login", *TOKEN_ROUTES}
    # The mint sits one slash away from its redemption and is *not* exempt.
    assert http_api._needs_a_session("/api/uploads") is True
    assert http_api._is_capability_path("/api/uploads") is False


def _mcp_request(app, *, token: str | None = None, session: str | None = None, host: str = None):
    """One raw request to ``/mcp`` with the app's lifespan actually running.

    The lifespan is not optional here and the reason is worth stating: the MCP
    transport's session manager is started there, and without it the route
    answers **500**, not a hang and not a 404. That is a plausible-looking
    green in a test that only ever asserts a refusal — every negative case
    below would still "pass" against a route that is simply broken. Running the
    lifespan is what makes the positive case available to prove otherwise.
    """
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if session is not None:
        headers["Cookie"] = f"{http_api.SESSION_COOKIE}={session}"

    async def run() -> httpx.Response:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url=host or BASE_URL) as client:
                return await client.post(
                    mcp_server.MCP_PATH,
                    headers=headers,
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                )

    return asyncio.run(run())


# ── The two surfaces share an origin and must not share a credential ──────────
#
# Before the MCP transport moved onto this app, a session cookie and an agent
# token could not be confused: they arrived at different processes over
# different transports. Now they arrive at the same origin and the only thing
# keeping them apart is that `/api` reads a cookie and `/mcp` reads a header.
# That is sound, and it is now a *claim* — so it is swept, in both directions.


def test_an_agent_token_opens_no_api_route(fresh_db):
    """A bearer token is not a session, on every route, derived from the table.

    An agent holding a perfectly valid token must get exactly as far on the
    human surface as a stranger does: nowhere. The route list is the input, so
    a route added tomorrow is covered without anyone remembering to add it —
    the same reason the attribution sweep reads the table rather than a list.
    """
    created = service.create_agent("bearer-probe", owner_human_id="owner", principal=owner())
    app = http_api.create_app()
    gated = [
        route.path
        for route in app.routes
        if route.path.startswith("/api") and http_api._needs_a_session(route.path)
    ]
    assert len(gated) >= 30, "the sweep must actually cover the API surface"

    # The token has to be *good*, or this test passes on a typo: every route
    # would 401 a nonsense credential too, and the sweep would be asserting
    # nothing about the boundary it is named for. Proving it opens `/mcp` is
    # what makes the 401s below mean "wrong surface" rather than "bad token".
    opens_mcp = _mcp_request(app, token=created.token)
    assert opens_mcp.status_code == 200, opens_mcp.text

    client = Client(app)  # no session cookie: the token is the only credential offered
    for path in gated:
        concrete = (
            path.replace("{id}", "whatever")
            .replace("{type}", "note")
            .replace("{path:path}", "x")
            .replace("{profile}", "thumb")
        )
        response = client.get(concrete, headers={"Authorization": f"Bearer {created.token}"})
        assert response.status_code == 401, (
            f"{path} answered {response.status_code} to a bearer token"
        )
        assert response.json()["error"]["type"] in {"Unauthorized", "InvalidCredentials"}


def test_a_session_cookie_opens_no_mcp_call(fresh_db):
    """The inverse: a logged-in human's cookie is not an agent credential.

    It matters because the cookie is the one credential a *browser* attaches by
    itself. If `/mcp` accepted it, every page the human visits would be one
    fetch away from the agent surface — which is the whole reason that surface
    is exempt from the origin checks in the first place.
    """
    app = http_api.create_app()
    service.set_human_password("owner", OWNER_PASSWORD, principal=owner())
    response = _mcp_request(app, session=_login(app))
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert "get_node" not in response.text


def test_the_mcp_path_is_exempt_from_exactly_two_checks_and_no_others(fresh_db):
    """The exemption is one path, not a shape, and it does not widen the guard.

    ``_is_mcp_path`` skips the same-origin proof and the content-type check.
    It must not skip the ``Host`` check — that is the DNS-rebinding defence and
    has nothing to do with which credential the request carries — and it must
    not spread to paths that merely start with the same characters.
    """
    assert http_api._is_mcp_path(mcp_server.MCP_PATH) is True
    for near_miss in ("/mcp/", "/mcp/x", "/mcpx", "/api/mcp", "/"):
        assert http_api._is_mcp_path(near_miss) is False, near_miss

    # The Host check still bites, credential or no credential.
    created = service.create_agent("host-probe", owner_human_id="owner", principal=owner())
    app = http_api.create_app()

    rebound = _mcp_request(app, token=created.token, host="http://evil.example")
    assert rebound.status_code == 400
    assert rebound.json()["error"]["type"] == "UntrustedHost"


def test_the_capability_routes_bind_no_identity_of_their_own(fresh_db):
    """They have no session by design, so they must not reach for one.

    ``_session_principal`` raises when the scope holds no verified principal —
    which is exactly the state these two run in — so a handler that called it
    would be a 500 on every redemption. Neither may write to the graph either:
    that needs a principal, and the only truthful one lives on the token row,
    where :func:`nodum.urls.consume` uses it.
    """
    endpoints = dict(_route_endpoints(http_api.create_app()))

    for path in TOKEN_ROUTES:
        source = inspect.getsource(endpoints[path])
        assert "_session_principal" not in source, path
        assert "_write(" not in source, path
        assert "principal=" not in source, path


@pytest.mark.skipif(find_spec("pypdfium2") is None, reason="the pdf extra is not installed")
def test_a_page_raster_reaches_the_rendition_route_through_its_colon(client, fresh_db):
    """``page:3`` is one path segment: Starlette's default convertor is ``[^/]+``.

    Worth an end-to-end test rather than a reading of the regex, because the
    colon is the kind of character a router, a client, or a proxy can each
    decide to treat as special.
    """
    ingested = _ok(client.post("/api/ingest", json={"path": str(PDF_FIXTURE)}))
    base = f"/api/assets/{ingested['asset']['hash']}/rendition"

    response = client.get(f"{base}/page:1")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == assets.RENDITION_MIME
    assert Image.open(io.BytesIO(response.content)).format == "WEBP"
    # Page 2 renders as well, and is a different raster.
    assert client.get(f"{base}/page:2").content != response.content
    # `page:0` is not a page and `page:99` is past the end of this document.
    assert client.get(f"{base}/page:0").status_code == 400
    assert client.get(f"{base}/page:99").status_code == 400
    # A PDF has no thumb: the profile families each name the kind they read.
    assert client.get(f"{base}/thumb").status_code == 400


def test_the_blocking_routes_run_their_service_call_off_the_event_loop(
    client, fresh_db, tmp_path, fixture_server, monkeypatch
):
    """M22: the login, upload, ingest and rendition calls never run on the loop.

    There is no way to assert "the loop was not blocked" from inside the
    process, but there is a way to assert the mechanism that keeps it free:
    each blocking service call must run on a worker thread, never the loop's.
    Every one of them is wrapped in a recorder that runs the real function and
    remembers the thread it ran on; the harness drives one ``asyncio.run`` per
    request on the main thread, so a call that stayed inline would record the
    main thread and fail the final assertion.

    ``verify_login`` joined the list for finding M2 and is the one that
    mattered most: argon2id is ~100 ms of deliberate work — spent on names
    that do not exist too, so the failure path costs what the success path
    costs — and it is the only route here an unauthenticated caller can
    reach. Inline, ten requests a second from anybody with a socket was a
    stopped server.
    """
    recordings: list[tuple[str, threading.Thread]] = []

    def off_loop(name: str):
        def decorate(func):
            def wrapper(*args, **kwargs):
                recordings.append((name, threading.current_thread()))
                return func(*args, **kwargs)

            return wrapper

        return decorate

    # POST /api/login — argon2id, on the one route with no session in front of
    # it. The `client` fixture has already set this password; setting it again
    # would end the session that fixture logged in with (service.set_password
    # drops a human's sessions on a credential change).
    monkeypatch.setattr(auth, "verify_login", off_loop("verify_login")(auth.verify_login))
    assert (
        Client(client.app)
        .post("/api/login", json={"name": "owner", "password": OWNER_PASSWORD})
        .status_code
        == 200
    )

    # POST /api/assets — registration streams the whole file into the store.
    monkeypatch.setattr(assets, "register_asset", off_loop("register_asset")(assets.register_asset))
    payload = _png_bytes()
    uploaded = _ok(client.post("/api/assets", files={"file": ("photo.png", payload, "image/png")}))
    assert uploaded["hash"] == hashlib.sha256(payload).hexdigest()

    # GET /api/assets/{id}/rendition/thumb — a miss renders (Pillow/pypdfium2).
    monkeypatch.setattr(assets, "get_rendition", off_loop("get_rendition")(assets.get_rendition))
    rendition = client.get(f"/api/assets/{uploaded['hash']}/rendition/thumb")
    assert rendition.status_code == 200, rendition.text
    assert Image.open(io.BytesIO(rendition.content)).format == "WEBP"

    # POST /api/ingest — both branches: a local path, then a server-side fetch.
    source = tmp_path / "hydrology.txt"
    source.write_text("Vercingetorix basin hydrology", encoding="utf-8")
    monkeypatch.setattr(ingest, "ingest_file", off_loop("ingest_file")(ingest.ingest_file))
    ingested = _ok(client.post("/api/ingest", json={"path": str(source), "title": "Basin"}))
    assert ingested["created"] is True
    assert ingested["source"]["title"] == "Basin"
    fixture_server.canned = (
        b"<html><body><p>Basin hydrology</p></body></html>",
        "text/html; charset=utf-8",
    )
    monkeypatch.setattr(ingest, "ingest_url", off_loop("ingest_url")(ingest.ingest_url))
    fetched = _ok(
        client.post("/api/ingest", json={"url": _fixture_url(fixture_server, "/article")})
    )
    assert fetched["extraction"]["handler"] == "html"

    # PUT /api/uploads/{token} — the capability route: no session at all.
    grant = _mint_upload(client, "scan.png", len(payload))["grant"]
    monkeypatch.setattr(ingest, "ingest_upload", off_loop("ingest_upload")(ingest.ingest_upload))
    redemption = _ok(Client(client.app).put(grant["url"], guard=False, content=payload))
    assert redemption["asset"]["hash"] == uploaded["hash"]  # same bytes: dedup

    recorded = {name for name, _ in recordings}
    assert recorded == {
        "verify_login",
        "register_asset",
        "get_rendition",
        "ingest_file",
        "ingest_url",
        "ingest_upload",
    }, f"every blocking call must have run: {recorded}"
    main = threading.main_thread()
    inline = [name for name, thread in recordings if thread is main]
    assert inline == [], f"blocking calls ran on the event loop: {inline}"


def test_healthz_reports_liveness_and_not_the_database_path(fresh_db):
    """A probe needs ``status``, not a filesystem tour.

    ``/healthz`` sits outside auth on purpose, so anything it says is said to
    everyone — and it used to say the absolute database path, disclosing a
    username and a layout. ``nodum serve`` prints the path at startup
    instead, where the operator is the only reader.
    """
    payload = _ok(Client(http_api.create_app()).get("/healthz"))
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
    hub = service.create_node(type="concept", title="Hub", principal=owner())
    service.create_node(type="note", title="Leaf", content="[[Hub]]", principal=owner())

    bare = _ok(client.get(f"/api/nodes/{hub.id}"))
    assert "nodes" not in bare

    neighborhood = _ok(client.get(f"/api/nodes/{hub.id}?depth=1"))
    assert neighborhood["root"] == hub.id
    assert len(neighborhood["nodes"]) == 2
    assert neighborhood["truncated"] is False
    assert client.get(f"/api/nodes/{hub.id}?depth=-1").status_code == 400
    assert client.get(f"/api/nodes/{hub.id}?depth=abc").status_code == 400


def test_edges_list_and_create(client, fresh_db):
    a = service.create_node(type="concept", title="A", principal=owner())
    b = service.create_node(type="concept", title="B", principal=owner())
    edge = _ok(
        client.post(
            "/api/edges",
            json={"src_id": a.id, "dst_id": b.id, "type": "relates_to", "confidence": 0.5},
        )
    )
    assert (edge["state"], edge["created_by"]) == ("active", OWNER_ACTOR)

    listing = _ok(client.get(f"/api/edges?node_id={a.id}&type=relates_to"))
    assert listing["count"] == 1
    assert client.post("/api/edges", json={"src_id": a.id, "dst_id": b.id}).status_code == 400
    bad = client.post(
        "/api/edges", json={"src_id": a.id, "dst_id": b.id, "type": "relates_to", "confidence": 5}
    )
    assert bad.status_code == 400


def test_edges_as_of_reads_the_validity_window(client, fresh_db):
    """The D2/B8 gate through the HTTP surface: `?as_of=` places a retired
    edge at the instants its window covered, and nowhere else."""
    a = service.create_node(type="concept", title="A", principal=owner())
    b = service.create_node(type="concept", title="B", principal=owner())
    edge = _ok(
        client.post(
            "/api/edges",
            json={"src_id": a.id, "dst_id": b.id, "type": "relates_to", "confidence": 0.5},
        )
    )
    # Use the edge-only operation; the route delegates to this same capability.
    retired = service.archive_edge(edge["id"], principal=owner())
    assert retired.valid_to

    conn = db.connect()
    try:
        conn.execute(
            "UPDATE edges SET valid_from = ?, valid_to = ? WHERE id = ?",
            ("2026-08-01 10:00:00", "2026-08-01 10:00:10", edge["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    assert _ok(client.get("/api/edges?state=active"))["edges"] == []
    mid = _ok(client.get("/api/edges?as_of=2026-08-01%2010:00:05"))
    assert [e["id"] for e in mid["edges"]] == [edge["id"]]
    assert _ok(client.get("/api/edges?as_of=2026-08-01%2010:00:20"))["edges"] == []


def test_subgraph_as_of_reads_the_validity_window(client, fresh_db):
    """`GET /api/graph/subgraph?as_of=` follows a retired edge at the instants
    its window covered; the default read stays the live graph."""
    a = service.create_node(type="concept", title="A", principal=owner())
    b = service.create_node(type="concept", title="B", principal=owner())
    edge = service.create_edge(a.id, b.id, "relates_to", principal=owner())
    service.transition(edge.id, "archive", principal=owner())
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE edges SET valid_from = ?, valid_to = ? WHERE id = ?",
            ("2026-08-01 10:00:00", "2026-08-01 10:00:10", edge.id),
        )
        conn.commit()
    finally:
        conn.close()

    live = _ok(client.get(f"/api/graph/subgraph?root={a.id}&depth=1"))
    assert {node["id"] for node in live["nodes"]} == {a.id}
    mid = _ok(client.get(f"/api/graph/subgraph?root={a.id}&depth=1&as_of=2026-08-01%2010:00:05"))
    assert {node["id"] for node in mid["nodes"]} == {a.id, b.id}
    later = _ok(client.get(f"/api/graph/subgraph?root={a.id}&depth=1&as_of=2026-08-01%2010:00:20"))
    assert {node["id"] for node in later["nodes"]} == {a.id}


def test_node_create_rejects_malformed_field_types(client, fresh_db):
    """M1: caller input that used to reach SQLite as a 500 is a 400 at the boundary.

    A dict body field was bound into SQLite directly, where a list or a dict
    is an ``InterfaceError`` — a 500 with a "database error" line. Now the
    input     model refuses the shapes first, and nothing is written either.
    """
    before = _events()
    for body in (
        {"type": "note", "props": ["a"]},
        {"type": {"a": 1}},
        {"type": "note", "content": {"x": 1}},
        {"type": "note", "title": ["x"]},
        {"type": "note", "surprise": 1},  # extra="forbid" on the input model
    ):
        assert client.post("/api/nodes", json=body).status_code == 400, body
    assert service.list_nodes(principal=owner()) == []
    assert _events() == before


def test_edge_create_rejects_malformed_confidence_and_props(client, fresh_db):
    """M1: a non-numeric ``confidence`` and a non-object ``props`` are 400s, and
    nothing is written (the old ``0 <= "abc"`` was a 500)."""
    a = service.create_node(type="concept", title="A", principal=owner())
    b = service.create_node(type="concept", title="B", principal=owner())
    before = _events()

    for body in (
        {"src_id": a.id, "dst_id": b.id, "type": "relates_to", "confidence": "abc"},
        {"src_id": a.id, "dst_id": b.id, "type": "relates_to", "props": ["x"]},
        {"dst_id": b.id, "type": "relates_to"},  # missing src_id stays a 400
        {"src_id": a.id, "dst_id": b.id, "type": "relates_to", "surprise": 1},
    ):
        assert client.post("/api/edges", json=body).status_code == 400, body
    assert service.list_edges(principal=owner()) == []
    assert _events() == before


def test_patch_null_semantics(client, fresh_db):
    """M2: ``title: null`` clears the title; ``content: null``/``props: null``
    are refused, and absent is distinct from null.

    ``title`` is nullable in the read model, so nulling it is the documented
    "clears the title" web affordance. ``content`` and ``props`` are not
    nullable — a null would corrupt read-back, so it is refused rather than
    stored.
    """
    node = _ok(client.post("/api/nodes", json={"type": "note", "title": "T", "content": "c"}))
    node_id = node["id"]

    cleared = _ok(client.patch(f"/api/nodes/{node_id}", json={"title": None}))
    assert cleared["title"] is None

    for body in ({"content": None}, {"props": None}, {"title": "x", "bogus": 1}):
        assert client.patch(f"/api/nodes/{node_id}", json=body).status_code == 400, body
    still = _ok(client.get(f"/api/nodes/{node_id}"))
    assert (still["title"], still["content"]) == (None, "c")  # the refusals wrote nothing

    retitled = _ok(client.patch(f"/api/nodes/{node_id}", json={"title": "Back"}))
    assert retitled["title"] == "Back"
    only_content = _ok(client.patch(f"/api/nodes/{node_id}", json={"content": "c2"}))
    assert only_content["title"] == "Back"  # a patch that does not name the title leaves it
    assert only_content["content"] == "c2"


def test_search_and_link_suggestions(client, fresh_db):
    service.create_node(
        type="note", title="Osmosis in plants", content="water moves", principal=owner()
    )

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


def test_state_param_absent_is_the_service_default():
    """B7: ``_state_param`` returns the service default for an absent parameter
    and ``None`` only for an explicit ``any`` — absent must not mean "every
    state" the way it did when ``None`` reached the service."""
    from starlette.datastructures import QueryParams

    param = http_api._state_param
    assert param(QueryParams("")) == "active"
    assert param(QueryParams("q=zebra")) == "active"
    assert param(QueryParams("state=active")) == "active"
    assert param(QueryParams("state=proposed")) == "proposed"
    assert param(QueryParams("state=archived")) == "archived"
    assert param(QueryParams("state=any")) is None


def test_search_defaults_to_active_until_state_any_is_said(client, fresh_db):
    """B7: a bare ``GET /api/search`` must not include proposed or archived rows.

    The HTTP surface used to hand ``state=None`` to the service for an absent
    parameter — the service's "every state" — where the CLI and the MCP server
    both default to ``active``. Absent is now the CLI's default, and ``any`` is
    the explicit opt-in to every state, exactly as on the other surfaces.
    """
    proposed = service.create_node(
        type="note", title="Zebra proposed", content="zebra stripes", principal=agent(AGENT)
    )
    archived = service.create_node(
        type="note", title="Zebra archived", content="zebra hooves", principal=owner()
    )
    service.transition(archived.id, "archive", principal=owner())
    active = service.create_node(
        type="note", title="Zebra active", content="zebra mane", principal=owner()
    )

    assert service.get_node(proposed.id, principal=owner()).state == "proposed"
    assert service.get_node(archived.id, principal=owner()).state == "archived"

    default_hits = _ok(client.get("/api/search?q=zebra"))["hits"]
    assert [hit["node_id"] for hit in default_hits] == [active.id]

    active_hits = _ok(client.get("/api/search?q=zebra&state=active"))["hits"]
    assert [hit["node_id"] for hit in active_hits] == [active.id]

    every_hits = _ok(client.get("/api/search?q=zebra&state=any"))["hits"]
    assert {hit["node_id"] for hit in every_hits} == {proposed.id, archived.id, active.id}


def test_nl_search_keeps_the_active_default(client, fresh_db):
    """B7: the rewrite branch shares ``_search_filters`` with the plain branch,
    so it keeps the same state default — one fixed source, two call sites."""
    proposed = service.create_node(
        type="note", title="Zebra proposed", content="zebra stripes", principal=agent(AGENT)
    )
    active = service.create_node(
        type="note", title="Zebra active", content="zebra mane", principal=owner()
    )
    llm.set_provider(_FakeLLM(_fake_completion({"terms": ["zebra"]})))

    body = _ok(client.get("/api/search?q=zebra+stripes&nl=1"))

    assert proposed.id not in {hit["node_id"] for hit in body["hits"]}
    assert [hit["node_id"] for hit in body["hits"]] == [active.id]


def test_graph_subgraph_and_path(client, fresh_db):
    hub = service.create_node(type="concept", title="Hub", principal=owner())
    leaves = [
        service.create_node(type="note", title=f"Leaf {i}", content="[[Hub]]", principal=owner())
        for i in range(3)
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
    first = service.create_node(type="note", title="One", principal=agent(AGENT))
    second = service.create_node(type="note", title="Two", principal=agent("other"))

    assert _ok(client.get("/api/review/queue"))["count"] == 2
    assert _ok(client.get(f"/api/review/queue?agent={AGENT}"))["count"] == 1
    assert _ok(client.get("/api/review/queue?kind=node&limit=1"))["count"] == 1

    accepted = _ok(client.post("/api/review/accept", json={"created_by": AGENT}))
    assert accepted["transitioned"] == [first.id]
    rejected = _ok(client.post("/api/review/reject", json={"ids": [second.id], "reason": "no"}))
    assert rejected["reason"] == "no"
    assert service.get_node(second.id, principal=owner()).state == "archived"
    reject_event = _events("node.reject")[0]
    assert reject_event.payload["reason"] == "no"


def test_the_review_queue_reports_a_space_for_every_kind_over_the_wire(client, fresh_db):
    """What the queue's space grouping (D4) actually consumes.

    The screen used to backfill this with a capped, chunked pass of `getNode`
    calls because only a proposed *node* stated a space. Every kind states one
    now, so a section header has its proposals under it without the client
    asking the server anything twice.
    """
    research = _ok(client.post("/api/spaces", json={"name": "research"}))["id"]
    near = service.create_node(type="concept", title="Near", principal=owner())
    far = service.create_node(type="concept", title="Far", space="research", principal=owner())
    proposer = agent(AGENT, grants={"meta": "read", "main": "suggest", research: "suggest"})
    service.create_edge(near.id, far.id, "supports", principal=proposer)
    service.update_node(near.id, content="revised", principal=proposer)

    queue = {row["kind"]: row for row in _ok(client.get("/api/review/queue"))["proposals"]}

    assert queue["edge"]["context"]["src"]["space_id"] == "main"
    assert queue["edge"]["context"]["dst"]["space_id"] == research
    assert queue["update"]["context"]["node"]["space_id"] == "main"


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
    assert events["events"][0]["actor"] == OWNER_ACTOR

    result = _ok(client.post("/api/undo", json={}))
    assert result["undone_op"] == "node.create"
    assert client.get(f"/api/nodes/{node['id']}").status_code == 404
    assert client.post("/api/undo", json={"seq": "nope"}).status_code == 400


# ── 6a. The smart endpoints: /ask, /summarize, and the natural-language search ─
#
# Every test here injects a fake provider through `llm.set_provider`, which the
# autouse `_no_llm_provider` fixture otherwise pins to *absent*. No test asserts
# on model output text: temperature-0 determinism was measured on one backend
# and is a property of that backend, not of the interface.


class _FakeLLM:
    """A provider that replays scripted completions over the ASGI boundary."""

    provider_id = "fake://provider"
    model_id = "fake-model"
    context_tokens = 4096
    thinking = llm.DEFAULT_THINKING
    thinking_applied = True

    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.structured_mode = llm.STRUCTURED_JSON_SCHEMA

    def estimate_prompt_tokens(self, messages, *, schema=None) -> int:
        return llm.estimate_prompt_tokens(messages)

    def output_reservation(self, max_output_tokens: int) -> int:
        """The real provider's rule: a reservation capped at a share of the
        window. An identity function here would model an endpoint that does not
        exist, and would leave the prompt no room at the shipped ceiling."""
        share = int(self.context_tokens * llm.OUTPUT_RESERVATION_FRACTION)
        return max(1, min(max_output_tokens, share))

    def chat(self, messages, *, schema=None, max_output_tokens, timeout, thinking=None):
        self.calls.append({"messages": list(messages), "schema": schema, "thinking": thinking})
        reply = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _fake_completion(payload: dict, *, finish_reason: str = "stop", prompt_tokens: int = 100):
    return llm.Completion(
        text=json.dumps(payload),
        prompt_tokens=prompt_tokens,
        output_tokens=20,
        finish_reason=finish_reason,
        model_id="fake-model",
        provider_id="fake://provider",
        context_tokens=4096,
        latency_ms=7,
    )


def _answerable(title: str = "Log compaction") -> str:
    return service.create_node(
        type="note",
        title=title,
        content="A compacted topic keeps the newest value per key, so it works as a state store.",
        principal=owner(),
    ).id


def test_ask_answers_with_citations_and_writes_nothing(client, fresh_db):
    node_id = _answerable()
    before = max(event.seq for event in service.list_events(owner(), limit=5000))
    llm.set_provider(
        _FakeLLM(_fake_completion({"answer": "It keeps the newest value.", "cited": ["1"]}))
    )

    body = _ok(client.post("/api/ask", json={"question": "compacted topic state store"}))

    assert body["answered"] is True
    assert body["answer"]
    assert [citation["node_id"] for citation in body["citations"]] == [node_id]
    assert body["considered"] == [node_id]
    assert body["used"]["model_id"] == "fake-model"
    after = max(event.seq for event in service.list_events(owner(), limit=5000))
    assert after == before, "the smart endpoints read; nothing writes by default (E1)"


def test_ask_computes_answered_from_citations_and_not_from_the_model(client, fresh_db):
    """The measured failure, driven over the wire.

    A schema-valid object whose citations name nothing this search returned is
    an unanswered question, whatever the object says, and the text does not
    come back with it.
    """
    _answerable()
    llm.set_provider(_FakeLLM(_fake_completion({"answer": "Yes, certainly.", "cited": ["id=n0"]})))

    body = _ok(client.post("/api/ask", json={"question": "compacted topic state store"}))

    assert body["answered"] is False
    assert body["answer"] is None
    assert body["citations"] == []
    assert body["unresolved"] == ["id=n0"]
    assert body["refusal"]


def test_ask_without_a_provider_is_a_refusal_that_names_the_variable(client, fresh_db, monkeypatch):
    """Not a 500, not a traceback, and not silence — the whole degradation rule.

    It resolves the provider the way a shipped install does rather than reusing
    the autouse fixture's stand-in reason: the sentence a human reads has to be
    the *real* one, and the fixture's is a test string that would make this pass
    while the shipped message said nothing useful.
    """
    monkeypatch.delenv(llm.ENV_MODEL, raising=False)
    llm.reset_provider()
    _answerable()
    body = _ok(client.post("/api/ask", json={"question": "compacted topic state store"}))

    assert body["answered"] is False
    assert body["used"]["available"] is False
    assert "NODUM_LLM_MODEL" in body["refusal"]


def test_ask_renders_an_output_ceiling_as_a_failure_not_an_empty_answer(client, fresh_db):
    _answerable()
    llm.set_provider(_FakeLLM(_fake_completion({"answer": "Kafka Str"}, finish_reason="length")))

    body = _ok(client.post("/api/ask", json={"question": "compacted topic state store"}))

    assert body["answered"] is False
    assert body["answer"] is None
    assert "ceiling" in body["refusal"]


def test_ask_reports_an_unreachable_provider_as_a_refusal(client, fresh_db):
    _answerable()
    llm.set_provider(_FakeLLM(llm.ProviderUnavailable("connection refused: localhost:11434")))

    body = _ok(client.post("/api/ask", json={"question": "compacted topic state store"}))

    assert body["answered"] is False
    assert "connection refused" in body["refusal"]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"question": ""},
        {"question": 7},
        {"question": "x", "k": 0},
        {"question": "x", "k": "six"},
    ],
)
def test_a_malformed_ask_is_a_400_rather_than_a_refusal(client, fresh_db, body: dict):
    """A client bug is not the model failing to answer, and must not read as one."""
    llm.set_provider(_FakeLLM(_fake_completion({"answer": "x", "cited": ["1"]})))
    assert client.post("/api/ask", json=body).status_code == 400


def test_summarize_reads_a_neighbourhood_and_writes_nothing(client, fresh_db):
    node_id = _answerable()
    before = max(event.seq for event in service.list_events(owner(), limit=5000))
    llm.set_provider(
        _FakeLLM(_fake_completion({"summary": "Compaction, briefly.", "cited": ["1"]}))
    )

    body = _ok(client.post("/api/summarize", json={"node_id": node_id}))

    assert body["summarized"] is True
    assert [citation["node_id"] for citation in body["citations"]] == [node_id]
    after = max(event.seq for event in service.list_events(owner(), limit=5000))
    assert after == before


def test_summarize_has_no_propose_flag_because_this_half_does_not_write(client, fresh_db):
    """5b-i is cut at the line where a model call causes a write, so an opt-in
    write is deliberately absent rather than accepted and ignored — an accepted
    flag that does nothing is exactly the since-deleted policies API's bug."""
    node_id = _answerable()
    before = max(event.seq for event in service.list_events(owner(), limit=5000))
    llm.set_provider(
        _FakeLLM(_fake_completion({"summary": "Compaction, briefly.", "cited": ["1"]}))
    )

    _ok(client.post("/api/summarize", json={"node_id": node_id, "propose": True}))

    after = max(event.seq for event in service.list_events(owner(), limit=5000))
    assert after == before
    assert not [row for row in service.list_proposals(principal=owner(), limit=50)]


def test_summarize_answers_the_right_question_about_a_node_that_does_not_exist(client, fresh_db):
    """With no provider this must still be a 404. Refusing an unreadable id with
    "no LLM provider configured" would answer the wrong question."""
    assert client.post("/api/summarize", json={"node_id": "nope"}).status_code == 404


def test_nl_search_layers_a_rewrite_over_the_ordinary_matcher(client, fresh_db):
    node_id = _answerable()
    llm.set_provider(_FakeLLM(_fake_completion({"terms": ["compacted", "topic"]})))

    body = _ok(client.get("/api/search?q=What+did+I+write+about+compacted+topics%3F&nl=1"))

    assert body["rewrite"]["applied"] is True
    assert body["rewrite"]["terms"] == ["compacted", "topic"]
    assert body["rewrite"]["original"] == "What did I write about compacted topics?"
    assert body["query"] == "compacted topic"
    assert [hit["node_id"] for hit in body["hits"]] == [node_id]


def test_nl_search_without_a_provider_still_searches(client, fresh_db, monkeypatch):
    monkeypatch.delenv(llm.ENV_MODEL, raising=False)
    llm.reset_provider()
    node_id = _answerable()
    body = _ok(client.get("/api/search?q=compacted+topic+state+store&nl=1"))

    assert body["rewrite"]["applied"] is False
    assert "NODUM_LLM_MODEL" in body["rewrite"]["refusal"]
    assert [hit["node_id"] for hit in body["hits"]] == [node_id]


def test_a_search_without_nl_is_byte_identical_to_the_command(client, fresh_db):
    """The rewrite is additive: an ordinary search is unchanged, and the CLI and
    the API still emit the same bytes for it."""
    _answerable()
    over_http = client.get("/api/search?q=compacted&limit=5")
    over_cli = runner.invoke(cli.app, ["search", "compacted", "--k", "5", "--as", "owner"])
    assert over_cli.exit_code == 0, over_cli.output
    assert over_http.content == over_cli.stdout.encode("utf-8")
    assert "rewrite" not in over_http.json()


# ── 6b. The dream journal: cycles, the runner, and rollback ──────────────────


def _curative_cycle(title: str = "Alpha") -> tuple[str, str]:
    """A closed one-op curative cycle, as ``(cycle_id, node_id)``.

    ``retype`` is the cheapest way to get a real cycle with a real graph write
    in it: every curative operation opens a cycle of its own when no runner has
    set one (decision C2), which is precisely what makes rollback the single
    reverse for the whole tier.
    """
    node = service.create_node(type="claim", title=title, principal=owner())
    return service.retype([node.id], "concept", principal=owner()).cycle_id, node.id


def _duplicate_pair(title: str = "Kafka Streams") -> None:
    """Two identically titled nodes, so a cycle has something to propose."""
    service.create_node(type="claim", title=title, principal=owner())
    service.create_node(type="claim", title=title, principal=owner())


def test_the_journal_runs_a_cycle_lists_it_and_reads_it_back(client, fresh_db):
    """The three reads a journal view is built from, over one real run.

    ``POST /api/cycles`` is the "run one now" the phase's exit criterion needs
    on this surface: the schedule is off unless configured, so without it the
    human's own journal could stay empty forever.
    """
    _duplicate_pair()

    ran = _ok(client.post("/api/cycles", json={}))
    assert ran["cycle"]["status"] == "completed"
    assert ran["cycle"]["trigger"] == "manual"
    assert ran["cycle"]["dry_run"] is False
    # Who asked is the session's human; who acted is the gardener, below.
    assert ran["cycle"]["triggered_by"] == OWNER_ACTOR
    assert {job["name"] for job in ran["report"]["jobs"]} == set(consolidate.JOBS)

    listing = _ok(client.get("/api/cycles"))
    assert [entry["id"] for entry in listing["cycles"]] == [ran["cycle"]["id"]]

    entry = _ok(client.get(f"/api/cycles/{ran['cycle']['id']}"))
    assert entry["cycle"] == ran["cycle"]
    assert set(entry["metrics"]) == {"before", "after"}
    assert "orphan_rate" in entry["metrics"]["before"]
    assert entry["events_truncated"] is False
    # The diff is the append-only log narrowed to the cycle, not a second copy
    # of it — and the writes inside are the gardener's, by design.
    assert entry["events"], "a cycle that proposed a duplicate wrote events"
    assert {event["cycle_id"] for event in entry["events"]} == {ran["cycle"]["id"]}
    assert {event["actor"] for event in entry["events"]} == {GARDENER_ACTOR}
    whole_log = _ok(client.get("/api/events?limit=500"))["events"]
    assert entry["events"] == whole_log[: len(entry["events"])]


def test_a_dry_run_is_in_the_journal_and_emitted_no_event(client, fresh_db):
    """A rehearsal is *in* the journal — the journal has to say which it was."""
    _duplicate_pair()

    ran = _ok(client.post("/api/cycles", json={"dry_run": True, "scope": "main"}))

    assert ran["cycle"]["dry_run"] is True
    assert ran["cycle"]["scope"] == "main"
    entry = _ok(client.get(f"/api/cycles/{ran['cycle']['id']}"))
    # The checkable form of "it changed nothing".
    assert entry["events"] == []
    assert entry["cycle"]["status"] == "completed"
    # A string is not a boolean: silently reading "false" as *run for real* is
    # exactly the coercion a rehearsal flag must not have.
    assert client.post("/api/cycles", json={"dry_run": "false"}).status_code == 400
    assert client.post("/api/cycles", json={"scope": "nowhere"}).status_code == 404


def test_a_cycle_rolls_back_over_http(client, fresh_db):
    """The one-click reversal, and the preview a confirm dialog asks for first."""
    cycle_id, node_id = _curative_cycle()
    assert _ok(client.get(f"/api/nodes/{node_id}"))["type"] == "concept"

    preview = _ok(client.post(f"/api/cycles/{cycle_id}/rollback", json={"dry_run": True}))
    assert (preview["dry_run"], preview["rollback_cycle_id"], preview["conflicts"]) == (
        True,
        None,
        [],
    )
    assert preview["reversed_events"]
    # A dry run opens no cycle and writes nothing at all.
    assert [entry["id"] for entry in _ok(client.get("/api/cycles"))["cycles"]] == [cycle_id]
    assert _ok(client.get(f"/api/nodes/{node_id}"))["type"] == "concept"

    result = _ok(client.post(f"/api/cycles/{cycle_id}/rollback", json={}))

    assert result["dry_run"] is False
    assert result["conflicts"] == []
    assert _ok(client.get(f"/api/nodes/{node_id}"))["type"] == "claim"
    entry = _ok(client.get(f"/api/cycles/{cycle_id}"))
    assert entry["cycle"]["status"] == "rolled_back"
    assert entry["cycle"]["rolled_back_by"] == result["rollback_cycle_id"]
    # The rollback is itself a journal entry, which is how it is reversed.
    assert _ok(client.get(f"/api/cycles/{result['rollback_cycle_id']}"))["cycle"]["trigger"] == (
        "rollback"
    )
    assert client.post("/api/cycles/nope/rollback", json={}).status_code == 404


def test_an_interrupted_cycle_is_abandoned_over_http_and_only_then_rolled_back(client, fresh_db):
    """The stuck-run door on the surface that owns the reversal.

    A cycle left ``running`` by a ``SIGKILL``, a power cut, or a shutdown that
    cancelled the nightly task in flight is not cosmetic: rollback refuses it
    (its event set is not closed) and ``undo`` refuses every event it stamped,
    so its writes were irreversible on **every** surface. ``abandon_cycle``
    existed in the service and no route reached it.
    """
    cycle = service.open_cycle(trigger="manual", principal=owner())
    with service.in_cycle(cycle.id):
        node = service.create_node(type="claim", title="Half-written", principal=owner())

    stuck = client.post(f"/api/cycles/{cycle.id}/rollback", json={})
    assert stuck.status_code == 400
    assert "still running" in stuck.json()["error"]["message"]

    abandoned = _ok(client.post(f"/api/cycles/{cycle.id}/abandon", json={}))

    assert abandoned["status"] == "failed"
    assert abandoned["report"]["abandoned"] is True
    assert abandoned["report"]["abandoned_by"] == OWNER_ACTOR
    # Which is what unlocks the reversal this surface exists to offer.
    _ok(client.post(f"/api/cycles/{cycle.id}/rollback", json={}))
    assert _ok(client.get(f"/api/cycles/{cycle.id}"))["cycle"]["status"] == "rolled_back"
    assert client.get(f"/api/nodes/{node.id}").status_code == 404

    # It is not a general "close this cycle": one that already said how it ended
    # is refused rather than having that record overwritten.
    refused = client.post(f"/api/cycles/{cycle.id}/abandon", json={})
    assert refused.status_code == 400
    assert "not running" in refused.json()["error"]["message"]
    assert client.post("/api/cycles/nope/abandon", json={}).status_code == 404


def test_the_kill_switch_is_a_route_and_it_is_not_the_abandon_route(client, fresh_db):
    """``POST /api/cycles/{id}/stop`` — the browser half of the kill switch (K1).

    ``service.request_stop`` shipped with migration ``0015`` and no route reached
    it, on the one surface that displays a running cycle. It takes the shape of
    ``/abandon`` — bodyless verb-POST, the cycle row back, 400 on a cycle that is
    not ``running``, 404 on an unknown id — and behaves like nothing else on it:
    the entry stays **running** and the run's writes are untouched. A stop that
    closed the row would be an abandon under another name, and the journal exists
    to keep "the operator stopped this" apart from "this process died".
    """
    cycle = service.open_cycle(trigger="manual", principal=owner())
    with service.in_cycle(cycle.id):
        node = service.create_node(type="claim", title="Half-written", principal=owner())

    stopped = _ok(client.post(f"/api/cycles/{cycle.id}/stop", json={}))

    assert stopped["status"] == "running"
    assert stopped["stop_requested"] is True
    assert stopped["stop_requested_by"] == OWNER_ACTOR
    assert stopped["stop_requested_at"] is not None
    # Nothing was reversed and nothing was closed — the two things it must not do.
    assert stopped["report"] is None
    assert client.get(f"/api/nodes/{node.id}").status_code == 200
    assert _ok(client.get(f"/api/cycles/{cycle.id}"))["cycle"]["stop_requested"] is True

    # Asking twice keeps the first asker, and answers 200 rather than refusing:
    # a switch that raised on the second press would make a human doubt the first.
    again = _ok(client.post(f"/api/cycles/{cycle.id}/stop", json={}))
    assert again["stop_requested_at"] == stopped["stop_requested_at"]

    # And the two verbs stay apart on the wire: a cycle that has said how it
    # ended has nothing left to obey a stop.
    _ok(client.post(f"/api/cycles/{cycle.id}/abandon", json={}))
    refused = client.post(f"/api/cycles/{cycle.id}/stop", json={})
    assert refused.status_code == 400
    assert "not running" in refused.json()["error"]["message"]
    assert client.post("/api/cycles/nope/stop", json={}).status_code == 404


def test_a_refused_rollback_is_a_409_that_names_the_rows(client, fresh_db):
    """Decision C4 over the wire: it refuses rather than clobbers, and says what.

    The body carries ``conflicts`` because that list is what the journal
    renders — a UI that had to parse the message back out could not offer the
    human the four rows that are in the way.
    """
    cycle_id, node_id = _curative_cycle()
    _ok(client.patch(f"/api/nodes/{node_id}", json={"title": "Edited after the cycle"}))

    response = client.post(f"/api/cycles/{cycle_id}/rollback", json={})

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["type"] == "RollbackConflict"
    assert node_id in error["message"]
    (conflict,) = error["conflicts"]
    assert conflict["kind"] == "node"
    assert conflict["row_id"] == node_id
    assert conflict["cycle_event_op"] == "node.retype"
    assert conflict["conflicting_op"] == "node.update"
    assert conflict["conflicting_actor"] == OWNER_ACTOR
    assert conflict["conflicting_cycle_id"] is None
    assert conflict["conflicting_seq"] > conflict["cycle_event_seq"]
    # Refused means nothing was written: the retype still stands.
    assert _ok(client.get(f"/api/nodes/{node_id}"))["type"] == "concept"

    # The dry run answers the same question without raising — the "would this
    # succeed?" a confirm dialog asks before it offers the button at all.
    preview = _ok(client.post(f"/api/cycles/{cycle_id}/rollback", json={"dry_run": True}))
    assert [row["row_id"] for row in preview["conflicts"]] == [node_id]


def test_a_conflict_with_another_cycle_names_that_cycle(client, fresh_db):
    """``conflicting_cycle_id`` is the field that tells a human where to start."""
    node = service.create_node(type="claim", title="Alpha", principal=owner())
    first = service.retype([node.id], "concept", principal=owner())
    second = service.retype([node.id], "person", principal=owner())

    response = client.post(f"/api/cycles/{first.cycle_id}/rollback", json={})

    assert response.status_code == 409
    (conflict,) = response.json()["error"]["conflicts"]
    assert conflict["conflicting_cycle_id"] == second.cycle_id


def test_the_journal_and_its_rollback_are_human_only(client, fresh_db):
    """Sessions here mint humans only, so the gate is met by construction — and
    enforced in the service regardless, which is what keeps it true on every
    other surface. A grant does not delegate writing recorded payloads back."""
    cycle_id, _ = _curative_cycle()
    granted = agent(AGENT, grants={"main": "edit"})

    for call in (
        lambda: service.rollback_cycle(cycle_id, principal=granted),
        lambda: service.get_cycle(cycle_id, principal=granted),
        lambda: service.list_cycles(principal=granted),
    ):
        with pytest.raises(service.GrantNotPermitted):
            call()

    assert http_api.EXCEPTION_STATUS[service.GrantNotPermitted] == 403
    anonymous = Client(client.app)
    assert anonymous.post(f"/api/cycles/{cycle_id}/rollback", json={}).status_code == 401
    assert anonymous.get("/api/cycles").status_code == 401


def test_an_unknown_principal_is_a_404_and_not_a_traceback(client, fresh_db):
    """The carried defect, on the route that can actually reach it.

    ``auth.UnknownPrincipal`` is a ``LookupError``, so it inherited no row in
    :data:`http_api.EXCEPTION_STATUS` and escaped as the generic 500 with a
    traceback in the server log. The runner loads the gardener by kind, so a
    file with no internal agent is the reachable flavour of it.
    """
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE agents SET kind = 'external', owner_human_id = 'owner' WHERE kind = 'internal'"
        )
        conn.commit()
    finally:
        conn.close()

    response = client.post("/api/cycles", json={})

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["type"] == "UnknownPrincipal"
    assert "0014" in error["message"]
    assert "Traceback" not in response.text


def test_a_scope_the_gardener_cannot_reach_says_so_to_the_browser(client, fresh_db):
    """Blocker 3's refusal, on the surface the blocker was found on.

    One click of the journal's scope picker is the whole reproduction: migration
    ``0014`` grants the gardener ``main`` and ``meta``, so every space a human
    creates afterwards is one the gardener cannot see, and the picker offers it
    anyway. ``nodum.consolidate`` answers that with the space's name and the
    exact command that fixes it — and over HTTP the sentence never arrived,
    because ``GrantNotPermitted`` is a ``PermissionError`` and
    ``_failure_message`` rewrote it as ``storage error: GrantNotPermitted``. The
    toast read ``GrantNotPermitted: storage error: GrantNotPermitted``: no space,
    no remedy, and nothing to do about either.
    """
    service.create_space("research", principal=owner())

    response = client.post("/api/cycles", json={"scope": "research"})

    assert response.status_code == 403, response.text
    error = response.json()["error"]
    assert error["type"] == "GrantNotPermitted"
    assert "storage error" not in response.text
    # The name the caller typed, never the id it resolved to, and the command.
    assert "'research'" in error["message"]
    assert f"nodum grant {GARDENER_AGENT_ID} research edit" in error["message"]
    # Granting it is the fix, and the fix works from here.
    service.grant(GARDENER_AGENT_ID, "research", "edit", principal=owner())
    assert _ok(client.post("/api/cycles", json={"scope": "research"}))["cycle"]["scope"]


#: How long the probe below waits for the event loop to answer while a cycle is
#: in flight. Generous for a loop that is free (it answers in microseconds) and
#: bounded for one that is not, so the broken shape fails in seconds rather than
#: hanging the suite.
LOOP_PROBE_TIMEOUT = 5.0


def test_running_a_cycle_does_not_stall_the_rest_of_the_server(fresh_db, monkeypatch):
    """``POST /api/cycles`` must not hold the event loop for the length of a cycle.

    The handler used to call the runner inline, like every other handler here.
    That is right for a read of a row and wrong for this one: a cycle is every
    job over every node in scope — 3.75 s measured against a real ``nodum
    serve`` on 450 nodes with no embedding provider, minutes on a graph with one
    — and the loop is single-threaded, so ``/healthz``, the SPA and every other
    tab froze for exactly that long. ``nodum.scheduler``'s own docstring is the
    argument, made for the nightly half and unmade for the half a human clicks.

    The proof is a probe fired **from inside the running cycle**: the patched
    runner asks the event loop to serve ``/healthz`` and waits a bounded time
    for the answer. A loop that is free answers at once; a loop executing the
    cycle cannot answer at all, so this fails with a timeout rather than passing
    slowly, which is what a test of "did it block?" has to do.
    """
    app = http_api.create_app()
    service.set_human_password("owner", OWNER_PASSWORD, principal=owner())
    session = _login(app)
    _duplicate_pair()

    real_consolidate = consolidate.consolidate
    loop_box: dict[str, object] = {}
    probes: list[httpx.Response] = []
    ran_off_the_loop: list[bool] = []

    def probing_runner(**kwargs):
        """Stand in for the runner and ask the loop for a page while we hold it."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            ran_off_the_loop.append(True)
        else:
            ran_off_the_loop.append(False)

        async def probe():
            return await loop_box["client"].get("/healthz")

        future = asyncio.run_coroutine_threadsafe(probe(), loop_box["loop"])
        probes.append(future.result(timeout=LOOP_PROBE_TIMEOUT))
        return real_consolidate(**kwargs)

    monkeypatch.setattr(consolidate, "consolidate", probing_runner)

    async def drive() -> httpx.Response:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http_client:
            loop_box["loop"] = asyncio.get_running_loop()
            loop_box["client"] = http_client
            return await http_client.post(
                "/api/cycles",
                json={},
                headers={
                    "Cookie": f"{http_api.SESSION_COOKIE}={session}",
                    **CLIENT_HEADERS,
                },
            )

    response = asyncio.run(drive())

    assert response.status_code == 200, response.text
    assert response.json()["cycle"]["status"] == "completed"
    # The loop answered a second request while the cycle was still running.
    assert [probe.status_code for probe in probes] == [200]
    # And it could, because the cycle was never on it in the first place.
    assert ran_off_the_loop == [True]


def test_a_second_cycle_is_a_409_and_not_a_400(client, fresh_db):
    """ "A cycle is already running" is a conflict with current state, not a bad request.

    ``CycleInProgress`` derives from ``ValueError``, so it rendered as a clean
    400 carrying the right sentence — and 400 tells a client its *request* was
    malformed, which this one was not. It is ``RollbackConflict``'s shape: the
    same request will succeed once the graph is done doing something else, which
    is what 409 means and what a client retries on.

    The guard held here is the real one and it is a **row**, not a lock: an open
    ``running`` consolidation cycle is what a second opener collides with, so
    this is the state another process would have left behind — which is the
    whole point of the guard living in the database. The refusal names that
    cycle and the way out of it, because a run a ``SIGKILL`` ended never closes
    itself and would otherwise block every later run behind advice nobody can
    carry out.
    """
    _duplicate_pair()
    blocking = service.open_cycle(trigger="manual", principal=owner())

    response = client.post("/api/cycles", json={})

    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["type"] == "CycleInProgress"
    assert "already running" in error["message"]
    assert blocking.id in error["message"]
    assert f"nodum cycle-abandon {blocking.id}" in error["message"]
    # And the refusal added nothing to the journal: the only entry is the one
    # that was already there, still open.
    listed = _ok(client.get("/api/cycles"))["cycles"]
    assert [(entry["id"], entry["status"]) for entry in listed] == [(blocking.id, "running")]


def test_a_cycles_event_window_is_bounded_and_says_when_it_bit(client, fresh_db):
    """A nightly run can emit thousands of events; the journal reads a window."""
    _duplicate_pair()
    ran = _ok(client.post("/api/cycles", json={}))

    entry = _ok(client.get(f"/api/cycles/{ran['cycle']['id']}?limit=1"))

    assert len(entry["events"]) == 1
    assert entry["events_truncated"] is True
    assert client.get(f"/api/cycles/{ran['cycle']['id']}?limit=nope").status_code == 400


def test_export_downloads_the_node_as_json(client, fresh_db):
    hub = service.create_node(type="concept", title="Hub", principal=owner())
    service.create_node(type="note", title="Leaf", content="[[Hub]]", principal=owner())

    response = client.get(f"/api/export/node/{hub.id}?depth=1")
    assert response.status_code == 200
    assert response.headers["content-disposition"] == f'attachment; filename="nodum-{hub.id}.json"'
    payload = json.loads(response.text)
    assert payload["root"] == hub.id
    assert len(payload["nodes"]) == 2


def test_me_is_the_sessions_human(client, fresh_db):
    me = _ok(client.get("/api/me"))
    assert me["id"] == "owner"
    assert me["name"] == "owner"
    assert me["has_password"] is True
    assert me["disabled"] is False


def test_humans_list_and_create(client, fresh_db):
    listed = _ok(client.get("/api/humans"))
    assert [(human["id"], human["name"]) for human in listed["humans"]] == [("owner", "owner")]
    assert listed["count"] == 1

    created = _ok(client.post("/api/humans", json={"name": "second"}))
    assert created["name"] == "second"
    assert created["has_password"] is False

    assert _ok(client.get("/api/humans"))["count"] == 2
    event = _events("human.create")[0]
    assert event.actor == OWNER_ACTOR
    assert event.payload["after"] == {"id": created["id"], "name": "second"}

    assert client.post("/api/humans", json={}).status_code == 400


def test_a_second_human_cannot_take_the_login_name_of_the_first(client, fresh_db):
    """This route could end a human's login over HTTP, with one well-formed body.

    `humans.name` is the login handle and `auth.verify_login` refuses a name
    that resolves to two accounts, so `POST /api/humans {"name": "owner"}` used
    to answer 200 and take `POST /api/login` away for `owner` for good — no
    route here removes or renames a human, and disabling the clone is no cure
    because the ambiguity is refused ahead of the `disabled` check.

    A taken name is a **409**, not a 400: the request is well-formed and the
    caller may retry with another one, exactly as a taken agent id and a taken
    space name already are.
    """
    clash = client.post("/api/humans", json={"name": "owner"})

    assert clash.status_code == 409
    assert clash.json()["error"]["type"] == "AccountExists"
    assert "a human named 'owner' already exists" in clash.json()["error"]["message"]
    assert _ok(client.get("/api/humans"))["count"] == 1
    # And the login the duplicate would have killed still answers.
    _login(client.app, "owner", OWNER_PASSWORD)


def test_human_password_disable_enable(client, fresh_db):
    second = service.create_human("second", principal=owner())

    assert client.post(f"/api/humans/{second.id}/password", json={}).status_code == 400
    missing = client.post("/api/humans/nope/password", json={"password": "second-pw"})
    assert missing.status_code == 404
    # A password under the floor, and one that is not even a string.
    for refused in ("pw", 12):
        response = client.post(f"/api/humans/{second.id}/password", json={"password": refused})
        assert response.status_code == 400

    assert _ok(
        client.post(f"/api/humans/{second.id}/password", json={"password": "second-pw"})
    ) == {
        "ok": True,
        "human_id": second.id,
    }
    # The new password logs the account in by name.
    session = _login(client.app, "second", "second-pw")

    assert _ok(client.post(f"/api/humans/{second.id}/disable")) == {
        "ok": True,
        "human_id": second.id,
        "disabled": True,
    }
    # Its sessions die at the next request.
    assert Client(client.app, session=session).get("/api/me").status_code == 401
    assert client.post("/api/humans/nope/disable").status_code == 404

    assert _ok(client.post(f"/api/humans/{second.id}/enable")) == {
        "ok": True,
        "human_id": second.id,
        "disabled": False,
    }
    _login(client.app, "second", "second-pw")


def test_agents_create_list_rotate_disable_enable(client, fresh_db):
    created = _ok(client.post("/api/agents", json={"name": "researcher"}))
    token = created["token"]
    assert token.startswith("ndm_")
    agent_row = created["agent"]
    assert agent_row["id"] == "researcher"
    assert agent_row["kind"] == "external"
    assert agent_row["owner_human_id"] == "owner"
    assert agent_row["has_token"] is True

    # The creation template grants read on meta — and nothing else.
    grants = _ok(client.get("/api/grants?agent=researcher"))
    assert [(row["space_id"], row["level"]) for row in grants["grants"]] == [("meta", "read")]

    # The shown token authenticates; it never reappears in a later body.
    assert auth.verify_agent_token(token).id == "researcher"
    listed = {row["id"]: row for row in _ok(client.get("/api/agents"))["agents"]}
    assert "token" not in listed["researcher"]
    # The gardener (migration 0014) lists beside it and holds no credential.
    assert listed[GARDENER_AGENT_ID]["kind"] == "internal"
    assert listed[GARDENER_AGENT_ID]["has_token"] is False

    rotated = _ok(client.post("/api/agents/researcher/token-rotate"))
    new_token = rotated["token"]
    assert rotated == {"agent_id": "researcher", "token": new_token}
    with pytest.raises(auth.InvalidCredentials):
        auth.verify_agent_token(token)
    assert auth.verify_agent_token(new_token).id == "researcher"
    assert client.post("/api/agents/nope/token-rotate").status_code == 404

    assert _ok(client.post("/api/agents/researcher/disable")) == {
        "ok": True,
        "agent_id": "researcher",
        "disabled": True,
    }
    with pytest.raises(auth.InvalidCredentials):
        auth.verify_agent_token(new_token)
    assert _ok(client.post("/api/agents/researcher/enable")) == {
        "ok": True,
        "agent_id": "researcher",
        "disabled": False,
    }
    assert auth.verify_agent_token(new_token).id == "researcher"
    assert client.post("/api/agents/nope/disable").status_code == 404
    assert client.post("/api/agents", json={}).status_code == 400


def test_grants_grant_list_and_revoke(client, fresh_db):
    service.create_agent("researcher", owner_human_id="owner", principal=owner())

    granted = _ok(
        client.post("/api/grants", json={"agent": "researcher", "space": "main", "level": "edit"})
    )
    assert granted["agent_id"] == "researcher"
    assert granted["space_id"] == "main"
    assert granted["level"] == "edit"

    everything = {
        (row["agent_id"], row["space_id"], row["level"])
        for row in _ok(client.get("/api/grants"))["grants"]
    }
    # The creation-template meta read, plus this one …
    assert {("researcher", "meta", "read"), ("researcher", "main", "edit")} <= everything
    # … alongside the gardener's own seeded rows, which are ordinary grants —
    # and the same `meta` read every other curating agent holds.
    assert {
        (GARDENER_AGENT_ID, "meta", "read"),
        (GARDENER_AGENT_ID, "main", "edit"),
    } <= everything
    filtered = _ok(client.get("/api/grants?agent=researcher"))
    assert filtered["count"] == 2
    assert _ok(client.get("/api/grants?agent=nobody")) == {"grants": [], "count": 0}

    bad_level = client.post(
        "/api/grants", json={"agent": "researcher", "space": "main", "level": "own"}
    )
    assert bad_level.status_code == 400
    unknown_space = client.post(
        "/api/grants", json={"agent": "researcher", "space": "nope", "level": "read"}
    )
    assert unknown_space.status_code == 404
    assert client.post("/api/grants", json={"agent": "researcher"}).status_code == 400

    revoked = _ok(client.post("/api/grants/revoke", json={"agent": "researcher", "space": "main"}))
    assert revoked == {"ok": True, "agent": "researcher", "space": "main"}
    remaining = _ok(client.get("/api/grants?agent=researcher"))
    assert [(row["space_id"], row["level"]) for row in remaining["grants"]] == [("meta", "read")]
    again = client.post("/api/grants/revoke", json={"agent": "researcher", "space": "main"})
    assert again.status_code == 404


def test_spaces_lists_active_spaces_only(client, fresh_db):
    """The grant-admin space picker: active spaces, archived ones drop out."""
    seeded = _ok(client.get("/api/spaces"))
    # `conventions` (migration 0016) is a real space, listed like any other.
    assert [(space["id"], space["type"]) for space in seeded["spaces"]] == [
        ("meta", "space"),
        ("main", "space"),
        ("conventions", "space"),
    ]

    space = _ok(client.post("/api/spaces", json={"name": "scratch"}))
    assert [row["id"] for row in _ok(client.get("/api/spaces"))["spaces"]] == [
        "meta",
        "main",
        "conventions",
        space["id"],
    ]

    service.transition(space["id"], "archive", principal=owner())
    remaining = _ok(client.get("/api/spaces"))
    assert [row["id"] for row in remaining["spaces"]] == ["meta", "main", "conventions"]


def test_a_structural_space_cannot_be_archived_over_http(client, fresh_db):
    """The refusal lives in the service, so both spellings of archive inherit it.

    The `/spaces` screen disables the button; that is copy, not a guard. This
    is the guard — and the generic node route is checked too, because a route
    that reaches the same row is the same hole.
    """
    for path in ("/api/spaces/main/archive", "/api/nodes/main/archive"):
        refused = client.post(path)
        assert refused.status_code == 400, path
        assert "cannot archive the 'main' space" in refused.json()["error"]["message"]

    assert client.post("/api/spaces/meta/archive").status_code == 400
    assert [row["id"] for row in _ok(client.get("/api/spaces"))["spaces"]] == [
        "meta",
        "main",
        "conventions",
    ]

    # Rename stays allowed: it moves the title, not the id everything depends on.
    renamed = _ok(client.post("/api/spaces/main/rename", json={"name": "trunk"}))
    assert (renamed["id"], renamed["title"]) == ("main", "trunk")
    landed = _ok(client.post("/api/nodes", json={"type": "note", "title": "still lands"}))
    assert landed["space_id"] == "main"


def test_two_spaces_cannot_share_a_name_over_http(client, fresh_db):
    """A space resolves by id *or* title, so a duplicate makes `?space=` ambiguous.

    A taken name is a **409**: the request is well-formed and the caller can
    retry with another name, exactly as a duplicate account id is
    (`AccountExists`). It was a 400 through the bare `ValueError` before.
    """
    _ok(client.post("/api/spaces", json={"name": "research"}))

    clash = client.post("/api/spaces", json={"name": "research"})
    assert clash.status_code == 409
    assert clash.json()["error"]["type"] == "SpaceNameTaken"
    assert "a space already answers to 'research'" in clash.json()["error"]["message"]
    # A name equal to another space's *id* is the same ambiguity, and is the half
    # no unique index can express.
    assert client.post("/api/spaces", json={"name": "main"}).status_code == 409

    other = _ok(client.post("/api/spaces", json={"name": "draft"}))
    rename = client.post(f"/api/spaces/{other['id']}/rename", json={"name": "research"})
    assert rename.status_code == 409
    assert [row["title"] for row in _ok(client.get("/api/spaces"))["spaces"]] == [
        "meta",
        "main",
        "conventions",
        "research",
        "draft",
    ]


def test_an_archived_space_keeps_its_name_and_the_refusal_says_so(client, fresh_db):
    """The name is held by something `GET /api/spaces` does not return.

    Which is exactly why the message has to name the state: the screen's own
    list cannot explain this refusal, so the words must. And the undo that the
    reservation protects — the only route back from an archive — now succeeds
    instead of serving a 500 from a bare `IntegrityError`.
    """
    space = _ok(client.post("/api/spaces", json={"name": "research"}))
    _ok(client.post(f"/api/spaces/{space['id']}/archive"))
    assert [row["id"] for row in _ok(client.get("/api/spaces"))["spaces"]] == [
        "meta",
        "main",
        "conventions",
    ]

    refused = client.post("/api/spaces", json={"name": "research"})
    assert refused.status_code == 409
    assert refused.json()["error"]["type"] == "SpaceNameTaken"
    assert "archived space already answers to 'research'" in refused.json()["error"]["message"]

    # And the restore the reservation exists for: undo the archive event.
    restored = _ok(client.post("/api/undo"))
    assert restored["undone_op"] == "node.archive"
    assert [row["id"] for row in _ok(client.get("/api/spaces"))["spaces"]] == [
        "meta",
        "main",
        "conventions",
        space["id"],
    ]


def test_undoing_a_rename_onto_a_taken_name_is_a_409_not_a_500(client, fresh_db):
    """The collision reserving titles does not remove, and it must stay mapped.

    `undo` restores a recorded row with a raw UPDATE, so a rename it reverses
    can put back a title another space has taken since. Unchecked that is
    `sqlite3.IntegrityError` — a 500 for a conflict the caller caused and can
    fix — instead of the 409 every other name clash answers with.
    """
    space = _ok(client.post("/api/spaces", json={"name": "scratch"}))
    _ok(client.post(f"/api/spaces/{space['id']}/rename", json={"name": "moved"}))
    rename_seq = max(event["seq"] for event in _ok(client.get("/api/events?limit=50"))["events"])
    replacement = _ok(client.post("/api/spaces", json={"name": "scratch"}))

    refused = client.post("/api/undo", json={"seq": rename_seq})
    assert refused.status_code == 409
    assert refused.json()["error"]["type"] == "UndoNotPossible"
    message = refused.json()["error"]["message"]
    assert f"a space already answers to 'scratch' (id {replacement['id']})" in message


def test_posting_a_space_typed_node_outside_meta_is_a_400(client, fresh_db):
    """This answered 200, and `space` is in the editor's type picker.

    So a human could nest a space inside ordinary territory with one click —
    `GET /api/spaces` then listed it and every space reference resolved it,
    while the grants governing it were the host space's. That is also what let
    the name refusal become an existence oracle on the rename path.
    """
    refused = client.post("/api/nodes", json={"type": "space", "title": "scratch", "space": "main"})

    assert refused.status_code == 400
    assert "a space must live in the 'meta' space" in refused.json()["error"]["message"]
    assert [row["id"] for row in _ok(client.get("/api/spaces"))["spaces"]] == [
        "meta",
        "main",
        "conventions",
    ]

    # Aimed at meta it is an ordinary space create, exactly as `POST /api/spaces` is.
    landed = _ok(client.post("/api/nodes", json={"type": "space", "title": "s", "space": "meta"}))
    assert landed["space_id"] == "meta"
    assert landed["id"] in [row["id"] for row in _ok(client.get("/api/spaces"))["spaces"]]


def test_undoing_a_space_create_that_now_holds_nodes_is_a_409_not_a_500(client, fresh_db):
    """`/spaces` put this one click away, and it answered with a server error.

    The create-reversal guarded `parent_id` children but not `nodes.space_id`,
    so the FK surfaced as `sqlite3.Error` → 500 for the ordinary "the graph has
    grown past this" case — the class of failure `/api/undo` was cleaned of.
    """
    space = _ok(client.post("/api/spaces", json={"name": "temp"}))
    create_seq = max(event["seq"] for event in _ok(client.get("/api/events?limit=50"))["events"])
    _ok(client.post("/api/nodes", json={"type": "note", "title": "n", "space": "temp"}))

    refused = client.post("/api/undo", json={"seq": create_seq})

    assert refused.status_code == 409
    assert refused.json()["error"]["type"] == "UndoNotPossible"
    assert "still holds 1 node" in refused.json()["error"]["message"]
    assert space["id"] in [row["id"] for row in _ok(client.get("/api/spaces"))["spaces"]]


def test_archiving_a_space_over_http_leaves_its_grant_inert_but_revocable(client, fresh_db):
    """The archive dialog's promise, end to end on the surface that makes it.

    `GET /api/spaces` stops carrying the space and its grant holders, so
    `/admin` is the only screen left that can show the delegation — and it must
    still be able to take it away.
    """
    space = _ok(client.post("/api/spaces", json={"name": "research"}))
    _ok(client.post("/api/agents", json={"name": "researcher"}))
    _ok(
        client.post(
            "/api/grants", json={"agent": "researcher", "space": "research", "level": "edit"}
        )
    )

    _ok(client.post(f"/api/spaces/{space['id']}/archive"))

    assert [row["id"] for row in _ok(client.get("/api/spaces"))["spaces"]] == [
        "meta",
        "main",
        "conventions",
    ]
    held = _ok(client.get("/api/grants?agent=researcher"))["grants"]
    assert (space["id"], "edit") in {(row["space_id"], row["level"]) for row in held}
    # Granting more on it is refused rather than silently conferring nothing.
    refused = client.post(
        "/api/grants", json={"agent": "researcher", "space": space["id"], "level": "read"}
    )
    assert refused.status_code == 400
    assert "cannot grant on the archived space" in refused.json()["error"]["message"]
    # And the grant can be taken away for good, which it could not before.
    _ok(client.post("/api/grants/revoke", json={"agent": "researcher", "space": space["id"]}))
    left = _ok(client.get("/api/grants?agent=researcher"))["grants"]
    assert space["id"] not in {row["space_id"] for row in left}


def test_spaces_carry_the_node_count_and_grant_holders(client, fresh_db):
    """What the ``/spaces`` screen reads: territory, not just a name."""
    space = _ok(client.post("/api/spaces", json={"name": "research"}))
    assert (space["type"], space["space_id"]) == ("space", "meta")

    _ok(client.post("/api/spaces", json={"name": "sandbox"}))
    _ok(client.post("/api/nodes", json={"type": "note", "title": "n", "space": "research"}))
    _ok(client.post("/api/agents", json={"name": "researcher"}))
    _ok(
        client.post(
            "/api/grants", json={"agent": "researcher", "space": "research", "level": "suggest"}
        )
    )

    listed = {row["title"]: row for row in _ok(client.get("/api/spaces"))["spaces"]}
    assert listed["research"]["node_count"] == 1
    assert [(g["agent_id"], g["level"]) for g in listed["research"]["grants"]] == [
        ("researcher", "suggest")
    ]
    assert listed["main"]["node_count"] == 0
    # The gardener's seeded grants (migration 0014) reach this screen like any
    # other agent's — which is what makes them revocable from it.
    assert [(g["agent_id"], g["level"]) for g in listed["main"]["grants"]] == [
        (GARDENER_AGENT_ID, "edit")
    ]
    # A space nobody was granted still reports an empty list, not a missing key.
    assert listed["sandbox"]["grants"] == []


def test_the_space_lifecycle_round_trips_over_http(client, fresh_db):
    space = _ok(client.post("/api/spaces", json={"name": "draft"}))

    renamed = _ok(client.post(f"/api/spaces/{space['id']}/rename", json={"name": "reference"}))
    assert (renamed["id"], renamed["title"]) == (space["id"], "reference")
    # Resolvable by name as well as by id — the same rule `--space` follows.
    assert (
        _ok(client.post("/api/spaces/reference/rename", json={"name": "library"}))["id"]
        == (space["id"])
    )

    archived = _ok(client.post(f"/api/spaces/{space['id']}/archive"))
    assert archived["state"] == "archived"
    assert [row["title"] for row in _ok(client.get("/api/spaces"))["spaces"]] == [
        "meta",
        "main",
        "conventions",
    ]
    assert client.post("/api/spaces", json={}).status_code == 400


def test_the_space_routes_refuse_a_node_that_is_not_a_space(client, fresh_db):
    """The URL says space, so it must not be a second way to edit any node."""
    note = service.create_node(type="note", title="not a space", principal=owner())

    assert client.post(f"/api/spaces/{note.id}/rename", json={"name": "x"}).status_code == 404
    assert client.post(f"/api/spaces/{note.id}/archive").status_code == 404
    assert service.get_node(note.id, principal=owner()).title == "not a space"


def test_a_node_create_lands_in_the_space_the_body_names(client, fresh_db):
    """B2: the write target. Absent, it is ``main`` — the service's own default."""
    _ok(client.post("/api/spaces", json={"name": "research"}))

    default = _ok(client.post("/api/nodes", json={"type": "note", "title": "default"}))
    targeted = _ok(
        client.post("/api/nodes", json={"type": "note", "title": "filed", "space": "research"})
    )

    assert default["space_id"] == "main"
    assert targeted["space_id"] != "main"
    assert targeted["created_by"] == OWNER_ACTOR  # a space is a place, never an identity
    assert (
        client.post("/api/nodes", json={"type": "note", "title": "x", "space": "nope"}).status_code
        == 404
    )
    assert (
        client.post("/api/nodes", json={"type": "note", "title": "x", "space": 7}).status_code
        == 400
    )


def test_the_listing_and_search_space_filters_narrow_the_read(client, fresh_db):
    _ok(client.post("/api/spaces", json={"name": "research"}))
    main = _ok(
        client.post(
            "/api/nodes", json={"type": "note", "title": "main memo", "content": "territory"}
        )
    )
    filed = _ok(
        client.post(
            "/api/nodes",
            json={
                "type": "note",
                "title": "research memo",
                "content": "territory",
                "space": "research",
            },
        )
    )

    everything = _ok(client.get("/api/nodes"))
    assert {row["id"] for row in everything["nodes"]} == {main["id"], filed["id"]}
    narrowed = _ok(client.get("/api/nodes?space=research"))
    assert [row["id"] for row in narrowed["nodes"]] == [filed["id"]]

    assert {hit["node_id"] for hit in _ok(client.get("/api/search?q=territory"))["hits"]} == {
        main["id"],
        filed["id"],
    }
    searched = _ok(client.get("/api/search?q=territory&space=research"))
    assert [hit["node_id"] for hit in searched["hits"]] == [filed["id"]]

    # An unknown space is a 404 from the listing and a 400 from search, never a
    # 500. The split is pre-existing and the `type` filter behaves identically:
    # the listing raises the service's `TypeNotFound`, while `nodum.search`
    # raises a plain `ValueError` rather than importing the service's exception
    # vocabulary. A client handles both.
    assert client.get("/api/nodes?space=nope").status_code == 404
    assert client.get("/api/search?q=territory&space=nope").status_code == 400


def test_include_meta_is_off_by_default_on_both_reads(client, fresh_db):
    """B3/D3: the type vocabulary stays out of content listings until asked for."""
    service.create_node(
        type="type",
        title="territory-kind",
        content="territory vocabulary",
        space="meta",
        props={"type_kind": "node"},
        principal=owner(),
    )
    _ok(client.post("/api/nodes", json={"type": "note", "title": "memo", "content": "territory"}))

    listed = _ok(client.get("/api/nodes"))
    assert [row for row in listed["nodes"] if row["space_id"] == "meta"] == []
    with_meta = _ok(client.get("/api/nodes?include_meta=true"))
    assert [row for row in with_meta["nodes"] if row["space_id"] == "meta"] != []

    assert [hit["title"] for hit in _ok(client.get("/api/search?q=territory"))["hits"]] == ["memo"]
    assert "territory-kind" in {
        hit["title"] for hit in _ok(client.get("/api/search?q=territory&include_meta=1"))["hits"]
    }
    assert client.get("/api/nodes?include_meta=maybe").status_code == 400


def test_the_agent_token_never_reaches_an_event_payload(client, fresh_db):
    """The HTTP layer must not break the service's show-once guarantee.

    The service keeps credential hashes out of event payloads; this asserts
    the routes added above don't reintroduce the token anywhere downstream —
    neither the creation token nor the rotated one appears in any event.
    """
    created = _ok(client.post("/api/agents", json={"name": "researcher"}))
    rotated = _ok(client.post("/api/agents/researcher/token-rotate"))

    payloads = json.dumps([event.payload for event in _events()])
    assert created["token"] not in payloads
    assert rotated["token"] not in payloads


def test_a_cancelled_upload_is_a_mapped_outcome_not_a_traceback(fresh_db):
    """``ClientDisconnect`` was unmapped, so every cancelled upload logged a traceback.

    Driven straight against the ASGI app: a cancelled upload is a client that
    stops sending, which reaches the application as ``http.disconnect`` on the
    receive channel — something no HTTP client library can be persuaded to
    reproduce faithfully.
    """
    app = http_api.create_app()
    session = _session_client(app).session
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
            (b"host", b"127.0.0.1:8600"),
            (b"content-type", b"multipart/form-data; boundary=x"),
            (b"cookie", f"{http_api.SESSION_COOKIE}={session}".encode()),
            (http_api.CLIENT_HEADER.encode(), b"nodum-tests"),
        ],
        "client": ("127.0.0.1", 51000),
        "server": ("127.0.0.1", 8600),
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
    """`nodum serve` builds the app and hands it to uvicorn on port 8600."""
    captured: dict = {}

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr("uvicorn.run", fake_run)
    result = runner.invoke(cli.app, ["serve"])

    assert result.exit_code == 0, result.output
    assert (captured["host"], captured["port"]) == ("127.0.0.1", 8600)
    assert Client(captured["app"]).get("/healthz").status_code == 200


def test_serve_announces_that_login_is_the_boundary(fresh_db, monkeypatch):
    """Any process may *attempt* a login; the password is the whole defence.

    That cannot be closed by origin control, so it is *stated* — the same
    philosophy as the banner it replaces, with the static bearer gone.
    """
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: None)

    result = runner.invoke(cli.app, ["serve"])

    assert result.exit_code == 0, result.output
    assert "password login" in result.output
    assert "human password" in result.output
    assert "#token" not in result.output


def test_serve_allows_a_non_loopback_bind(fresh_db, monkeypatch):
    """Decided 2026-07-25: login is the boundary, so a LAN bind is allowed.

    The old ``--token``-gated refusal is gone with the flag. The one thing
    the bind still decides is the session cookie's ``Secure`` flag: loopback
    is plain HTTP (a ``Secure`` cookie would never be stored), a LAN bind
    fronts TLS.
    """
    captured: dict = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: captured.update(app=app))

    result = runner.invoke(cli.app, ["serve", "--host", "0.0.0.0"])

    assert result.exit_code == 0, result.output
    service.set_human_password("owner", OWNER_PASSWORD, principal=owner())
    response = Client(captured["app"]).post(
        "/api/login", json={"name": "owner", "password": OWNER_PASSWORD}
    )
    assert response.status_code == 200, response.text
    assert "secure" in response.headers["set-cookie"].lower()


def test_serve_exits_1_when_the_port_is_in_use(fresh_db, monkeypatch):
    """uvicorn catches the failed bind itself and exits 3; the contract says 1."""

    def fails_to_start(app, **kwargs):
        raise SystemExit(3)

    monkeypatch.setattr("uvicorn.run", fails_to_start)
    result = runner.invoke(cli.app, ["serve", "--port", "8600"])

    assert result.exit_code == 1
    assert "could not serve on 127.0.0.1:8600" in result.output


def test_serve_allows_extra_hosts_on_the_command_line(fresh_db, monkeypatch):
    """``--allow-host`` is how a reverse proxy or a LAN name gets in."""
    captured: dict = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: captured.update(app=app))

    result = runner.invoke(
        cli.app,
        ["serve", "--host", "0.0.0.0", "--allow-host", "nodum.lan"],
    )

    assert result.exit_code == 0, result.output
    client = _session_client(captured["app"])
    assert client.get("/api/types", headers={"Host": "nodum.lan"}).status_code == 200
    assert client.get("/api/types", headers={"Host": "other.example"}).status_code == 400


# ── Settings over HTTP ────────────────────────────────────────────────────────

#: Distinctive enough that grepping a whole response body for it means something
#: (the same value tests/test_settings.py sweeps the CLI with).
SECRET = "sk-test-000111222333444555666777888999"


def test_get_settings_is_byte_identical_to_config_list(client, fresh_db):
    """Envelope parity: the route renders `nodum config list` verbatim.

    Reinstating a hand-built body (or list_envelope) on the route breaks the
    byte-equality below.
    """
    service.apply_settings({settings.LLM_MODEL: "test-model"}, principal=owner())

    result = runner.invoke(cli.app, ["config", "list"])
    assert result.exit_code == 0, result.output

    response = client.get("/api/settings")
    assert response.status_code == 200
    assert response.content == result.stdout.encode("utf-8")
    payload = response.json()
    assert payload["count"] == len(payload["settings"]) == 24
    assert payload["path"], "the GET carries the absolute path (deliberate, unlike /healthz)"


def test_put_settings_writes_removes_and_reports_rows(client, fresh_db):
    """A partial body: absent keys untouched, null removes, strings store."""
    service.apply_settings(
        {settings.LLM_THINKING: "low", settings.LLM_CYCLE_BUDGET: "100"},
        principal=owner(),
    )

    response = client.put(
        "/api/settings",
        json={settings.LLM_CYCLE_BUDGET: "200", settings.LLM_THINKING: None},
    )
    assert response.status_code == 200, response.text
    rows = response.json()["changes"]
    assert [row["key"] for row in rows] == [settings.LLM_CYCLE_BUDGET, settings.LLM_THINKING]
    by_key = {row["key"]: row for row in rows}
    assert by_key[settings.LLM_CYCLE_BUDGET]["changed"] is True
    assert by_key[settings.LLM_CYCLE_BUDGET]["value"] == "200"
    assert by_key[settings.LLM_THINKING]["stored"] is False
    assert settings.stored_values()[settings.LLM_CYCLE_BUDGET] == "200"
    assert settings.LLM_THINKING not in settings.stored_values()


def test_put_settings_refusing_the_second_key_leaves_the_file_identical(client, fresh_db):
    """The PUT is atomic: one invalid key writes nothing at all."""
    service.apply_settings({settings.AUDIO_MODEL: "base"}, principal=owner())
    path = Path(client.get("/api/settings").json()["path"])
    original = path.read_text(encoding="utf-8")

    response = client.put(
        "/api/settings",
        json={settings.LLM_MODEL: "test-model", settings.LLM_CONTEXT_TOKENS: "NaN"},
    )

    assert response.status_code == 400
    assert path.read_text(encoding="utf-8") == original


def test_put_settings_on_an_environment_pinned_key_is_409_and_writes_nothing(
    client, fresh_db, monkeypatch
):
    """A pin is a conflict with current state, not a malformed request.

    Reverting SettingPinned to a plain SettingRefused (or dropping its
    EXCEPTION_STATUS row) fails this with a 400.
    """
    monkeypatch.setenv(settings.LLM_THINKING, "low")
    service.apply_settings({settings.AUDIO_MODEL: "base"}, principal=owner())
    path = Path(client.get("/api/settings").json()["path"])
    original = path.read_text(encoding="utf-8")

    response = client.put("/api/settings", json={settings.LLM_THINKING: "medium"})

    assert response.status_code == 409, response.text
    assert response.json()["error"]["type"] == "SettingPinned"
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    ("body", "sentence"),
    [
        ({"NOT_A_SETTING": "x"}, "NOT_A_SETTING"),
        ({settings.LLM_CONTEXT_TOKENS: "not a number"}, "must be a whole number"),
        ({settings.LLM_MODEL: ""}, "unset it instead to fall back"),
        ({settings.LLM_API_KEY: "sk-test\nsecond-line"}, "control characters"),
    ],
)
def test_put_settings_refusals_carry_the_seams_sentence(client, fresh_db, body, sentence):
    """Every 400 quotes SP1's own refusal text, never a value it refused."""
    response = client.put("/api/settings", json=body)
    assert response.status_code == 400
    assert sentence in response.json()["error"]["message"]
    assert "sk-test" not in response.text


def test_put_settings_with_an_empty_body_touches_nothing(client, fresh_db):
    response = client.put("/api/settings", json={})
    assert response.status_code == 200
    assert response.json() == {"changes": [], "count": 0}


def test_put_settings_refuses_an_env_only_key_with_its_own_sentence(client, fresh_db):
    response = client.put("/api/settings", json={"NODUM_PUBLIC_URL": "https://x.example"})
    assert response.status_code == 400
    assert "cannot be stored" in response.json()["error"]["message"]


def test_delete_setting_answers_changed_false_for_a_key_the_file_never_carried(client, fresh_db):
    response = client.delete(f"/api/settings/{settings.LLM_CYCLE_BUDGET}")
    assert response.status_code == 200
    assert response.json()["changed"] is False

    service.apply_settings({settings.LLM_CYCLE_BUDGET: "300"}, principal=owner())
    response = client.delete(f"/api/settings/{settings.LLM_CYCLE_BUDGET}")
    assert response.status_code == 200
    assert response.json()["changed"] is True
    assert settings.LLM_CYCLE_BUDGET not in settings.stored_values()


def test_adopt_env_stores_pinned_values_without_moving_provenance(client, fresh_db, monkeypatch):
    """The cutover round-trip, asserted at every end it touches.

    `stored` flips true while provenance stays `environment`; one `settings.set`
    event per adopted key; the secret's payload reads set/unset; a value the
    registry refuses is skipped and named rather than failing the batch.
    """
    monkeypatch.setenv(settings.LLM_MODEL, "test-model")
    monkeypatch.setenv(settings.LLM_API_KEY, "sk-adopt-000111222333")
    monkeypatch.setenv(settings.LLM_CONTEXT_TOKENS, "not a number")

    before = {
        event.seq
        for event in service.list_events(owner(), limit=5000)
        if event.op.startswith("settings.")
    }

    response = client.post("/api/settings/adopt-env")

    assert response.status_code == 200, response.text
    payload = response.json()
    adopted = {row["key"]: row for row in payload["adopted"]}
    assert set(adopted) == {settings.LLM_MODEL, settings.LLM_API_KEY}
    for row in adopted.values():
        assert row["changed"] is True
        assert row["stored"] is True
        assert row["provenance"] == "environment"
    assert adopted[settings.LLM_API_KEY]["value"] is None, "a secret carries no value"
    assert adopted[settings.LLM_API_KEY]["set"] is True
    assert [row["key"] for row in payload["skipped"]] == [settings.LLM_CONTEXT_TOKENS]

    stored = settings.stored_values()
    assert stored[settings.LLM_MODEL] == "test-model"
    assert stored[settings.LLM_API_KEY] == "sk-adopt-000111222333"

    events = [
        event
        for event in service.list_events(owner(), limit=5000)
        if event.op == "settings.set" and event.seq not in before
    ]
    assert sorted(event.payload["key"] for event in events) == [
        settings.LLM_API_KEY,
        settings.LLM_MODEL,
    ], "one settings.set event per adopted key"
    secret_event = next(e for e in events if e.payload["key"] == settings.LLM_API_KEY)
    assert secret_event.payload["after"] == "set"
    assert secret_event.actor == OWNER_ACTOR


def test_a_settings_write_runs_off_the_event_loop(client, fresh_db, tmp_path):
    """The plan's probe: a child holds the flock, a PUT waits, /healthz answers.

    Both requests run on **one** event loop — the whole point — and the PUT
    task is created **first**: an inline handler never suspends, so it blocks
    the loop for the whole flock hold and healthz cannot run until the PUT has
    finished. (Two shapes were tried and discarded: separate ``asyncio.run``
    calls per request, which share no loop and pass vacuously; and a fixed
    sleep between the two tasks, which absorbs exactly the stall it is here to
    detect.)
    """
    lock_path = Path(client.get("/api/settings").json()["path"]).with_suffix(".env.lock")
    script = (
        "import fcntl, sys, time\n"
        "handle = open(sys.argv[1], 'w')\n"
        "fcntl.flock(handle, fcntl.LOCK_EX)\n"
        "print('locked', flush=True)\n"
        "time.sleep(3)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", script, str(lock_path)], stdout=subprocess.PIPE)
    try:
        assert child.stdout.readline().strip() == b"locked"

        async def drive() -> tuple[httpx.Response, httpx.Response, bool]:
            transport = httpx.ASGITransport(app=client.app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
                headers = {
                    "Cookie": f"{http_api.SESSION_COOKIE}={client.session}",
                    **CLIENT_HEADERS,
                    "Content-Type": "application/json",
                }
                # The PUT task is created first and runs to its first real
                # suspension; healthz is created second. An inline handler
                # never suspends — it blocks the loop inside the flock hold —
                # so healthz then cannot run until the PUT finishes, and
                # `put_still_pending` below is the assertion that catches it.
                put_task = asyncio.create_task(
                    http.put(
                        "/api/settings", json={settings.LLM_CYCLE_BUDGET: "400"}, headers=headers
                    )
                )
                health_task = asyncio.create_task(http.get("/healthz"))
                health = await health_task
                put_still_pending = not put_task.done()
                return health, await put_task, put_still_pending

        health, put_response, put_still_pending = asyncio.run(drive())

        assert health.status_code == 200
        assert put_still_pending, "the PUT finished without waiting for the flock"
        assert put_response.status_code == 200, put_response.text
    finally:
        child.wait(timeout=10)


def test_the_secret_sweep_over_every_http_settings_surface(client, fresh_db):
    """R2-B4 widened to HTTP: the export-shaped secret line reaches no response.

    The file's first line is the likeliest hand-edit mistake, carried over from
    a shell profile. It makes the file *unreadable*, which exercises all three
    surfaces at once: GET (resolution steps around it), the write paths' error
    bodies (`_failure_message` passes domain text through verbatim), and
    /api/events.
    """
    path = Path(client.get("/api/settings").json()["path"])
    path.write_text(f"export {settings.LLM_API_KEY}={SECRET}\n", encoding="utf-8")

    get_response = client.get("/api/settings")
    put_response = client.put("/api/settings", json={settings.LLM_CYCLE_BUDGET: "100"})
    delete_response = client.delete(f"/api/settings/{settings.LLM_CYCLE_BUDGET}")
    adopt_response = client.post("/api/settings/adopt-env")
    events_response = client.get("/api/events?limit=50")
    bad_value_response = client.put("/api/settings", json={settings.LLM_API_KEY: SECRET})
    # SP4: the export path joins the sweep — the redacted stream and, more
    # importantly, its *error bodies* (a wrong step-up password must not echo
    # anything the file held).
    export_redacted = client.post("/api/settings/export", json={})
    export_refused = client.post(
        "/api/settings/export", json={"include_secrets": True, "password": "nope"}
    )

    assert get_response.status_code == 200
    unreadable = get_response.json()["unreadable"]
    assert unreadable and "line 1" in unreadable
    assert put_response.status_code == 400, put_response.text
    assert delete_response.status_code == 400
    # adopt has nothing to adopt (a clean environment), so it never reaches the
    # file and answers 200 over an unreadable one
    assert adopt_response.status_code == 200
    assert events_response.status_code == 200
    assert bad_value_response.status_code == 400
    assert export_redacted.status_code == 200
    assert export_refused.status_code == 401

    for response in (
        get_response,
        put_response,
        delete_response,
        adopt_response,
        events_response,
        bad_value_response,
        export_redacted,
        export_refused,
    ):
        assert SECRET not in response.text, response.text[:400]
        assert f"{settings.LLM_API_KEY}=" not in response.text


# ── SP4: POST /api/settings/export — the streamed, step-up-authenticated file ─


def _export(client, body: dict) -> httpx.Response:
    return client.post("/api/settings/export", json=body)


def test_export_streams_the_file_with_the_download_header_set(client, fresh_db):
    settings.set_value(settings.LLM_MODEL, "exported-model")
    response = _export(client, {})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/octet-stream"
    assert (
        response.headers["content-disposition"]
        == f'attachment; filename="{settings.EXPORT_FILENAME}"'
    )
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert 'NODUM_LLM_MODEL="exported-model"' in response.text


def test_export_is_redacted_unless_asked_and_always_event_logged(client, fresh_db):
    settings.set_value(settings.LLM_API_KEY, SECRET)

    redacted = _export(client, {})
    assert redacted.status_code == 200
    assert SECRET not in redacted.text
    assert f"# {settings.LLM_API_KEY} is set but omitted" in redacted.text

    with_secrets = _export(client, {"include_secrets": True, "password": OWNER_PASSWORD})
    assert with_secrets.status_code == 200, with_secrets.text
    assert f'{settings.LLM_API_KEY}="{SECRET}"' in with_secrets.text

    events = [row for row in _events() if row.op == "settings.export"]
    assert [row.payload["include_secrets"] for row in events] == [True, False]  # newest first
    assert SECRET not in json.dumps([row.payload for row in events])
    assert all(row.actor == OWNER_ACTOR for row in events)


def test_step_up_refuses_a_wrong_password_and_logs_no_export(client, fresh_db):
    settings.set_value(settings.LLM_API_KEY, SECRET)
    response = _export(client, {"include_secrets": True, "password": "wrong password"})
    assert response.status_code == 401, response.text
    assert SECRET not in response.text
    assert not [row for row in _events() if row.op == "settings.export"]
    # The miss is on the login audit trail, where every password check lands.
    failures = [row for row in _events("human.login_failed")]
    assert len(failures) == 1
    assert failures[0].payload["name"] == "owner"


def test_include_secrets_without_a_password_is_a_400(client, fresh_db):
    response = _export(client, {"include_secrets": True})
    assert response.status_code == 400, response.text


def test_step_up_password_honours_the_login_ceiling(client, fresh_db):
    """Parity with login includes the work bound: no unbounded argon2 input."""
    response = _export(client, {"include_secrets": True, "password": "x" * 5000})
    assert response.status_code == 400, response.text
    assert "at most" in response.text


def test_step_up_lockout_is_parity_with_login_not_a_copy(client, fresh_db):
    """Five step-up misses lock the name exactly as five login misses would.

    The check runs through the login path's own lockout query, so the two
    surfaces share one counter: after five export refusals the *login route
    itself* answers 429, and a sixth export with the correct password is
    refused up front without spending argon2.
    """
    for _ in range(service.LOGIN_MAX_FAILED_ATTEMPTS):
        response = _export(client, {"include_secrets": True, "password": "still wrong"})
        assert response.status_code == 401, response.text

    login = Client(client.app).post(
        "/api/login", json={"name": "owner", "password": OWNER_PASSWORD}
    )
    assert login.status_code == 429, login.text

    locked = _export(client, {"include_secrets": True, "password": OWNER_PASSWORD})
    assert locked.status_code == 429, locked.text
    assert not [row for row in _events() if row.op == "settings.export"]


# ── SP5: the embedding variables over HTTP, coupled to the vector rebuild ─────


def _embedding_stub(monkeypatch):
    """A fastembed stand-in: 384-dim zero vectors, no model download.

    Makes ``import fastembed`` succeed and replaces ``FastembedProvider``, so
    ``embeddings.resolution()`` runs the real resolve path — the model name
    still comes from the settings ladder, exactly as it does with fastembed
    installed — then drops the suite's pin so resolution is live.
    """

    class Stub:
        dimensions = embeddings.EMBEDDING_DIMS

        def __init__(self, model_name, *, cache_dir=None):
            self.model_id = model_name
            self.cache_dir = cache_dir

        def embed(self, texts):
            return [[0.0] * self.dimensions for _ in texts]

    monkeypatch.setattr(embeddings, "FastembedProvider", Stub)
    monkeypatch.setitem(sys.modules, "fastembed", ModuleType("fastembed"))
    embeddings.reset_provider()


def test_put_settings_can_write_the_embedding_rows(client, fresh_db, monkeypatch):
    """The two embedding names are ordinary storable settings now."""
    _embedding_stub(monkeypatch)

    response = client.put(
        "/api/settings",
        json={settings.EMBED_MODEL: "web-picked-model", settings.EMBED_DOWNLOAD: "1"},
    )
    assert response.status_code == 200, response.text
    by_key = {row["key"]: row for row in response.json()["changes"]}
    assert by_key[settings.EMBED_MODEL]["value"] == "web-picked-model"
    assert by_key[settings.EMBED_MODEL]["provenance"] == settings.FROM_FILE
    assert by_key[settings.EMBED_DOWNLOAD]["value"] == "1"

    rows = {row["key"]: row for row in client.get("/api/settings").json()["settings"]}
    assert rows[settings.EMBED_MODEL]["writable"] is True
    assert rows[settings.EMBED_MODEL]["default"] == embeddings.DEFAULT_MODEL
    assert rows[settings.EMBED_DOWNLOAD]["writable"] is True
    assert rows[settings.EMBED_DOWNLOAD]["kind"] == "gate"
    # And the env-only cache name is still refused.
    refused = client.put("/api/settings", json={settings.EMBED_CACHE: "/tmp/models"})
    assert refused.status_code == 400
    assert "path on the server's own disk" in refused.text


def test_get_settings_carries_the_embedding_state_and_the_rebuild_clears_it(
    client, fresh_db, monkeypatch
):
    """The mixed-model coupling, end to end over HTTP.

    Chunks embedded with model A; a model change to B; the settings report
    says the chunks are invisible and offers the rebuild; the rebuild
    re-embeds with B and the note clears — every step through the real routes,
    with the provider resolution driven by the seam.
    """
    _embedding_stub(monkeypatch)
    embeddings.get_provider()  # resolve the default model up front

    for index in range(2):
        service.create_node(
            type="note",
            title=f"Seeded node {index}",
            content=" ".join(f"word{index}-{w}" for w in range(embeddings.CHUNK_WORDS + 20)),
            principal=owner(),
        )
    projectors.run_projectors(names=["vec"])
    assert embeddings.get_provider() is not None
    assert embeddings.get_provider().model_id == embeddings.DEFAULT_MODEL

    before = client.get("/api/settings").json()
    assert before["mixed_model_note"] is None
    assert before["embed_chunks"] >= 2

    changed = client.put("/api/settings", json={settings.EMBED_MODEL: "model-b"})
    assert changed.status_code == 200, changed.text

    mixed = client.get("/api/settings").json()
    assert mixed["mixed_model_note"] is not None
    assert "invisible to search" in mixed["mixed_model_note"]
    assert mixed["embed_chunks"] == before["embed_chunks"]
    assert embeddings.get_provider().model_id == "model-b"

    rebuild = client.post("/api/projectors/vec/rebuild")
    assert rebuild.status_code == 200, rebuild.text
    assert rebuild.json()["name"] == "vec"

    clean = client.get("/api/settings").json()
    assert clean["mixed_model_note"] is None, clean["mixed_model_note"]
    assert clean["embed_chunks"] == before["embed_chunks"]

    events = _events("projector.rebuild")
    assert len(events) == 1
    assert events[0].actor == OWNER_ACTOR
    assert events[0].payload["applied"] >= 2
    # And search finds the corpus again — the BM25 arm on the seeded titles
    # (the stub's zero vectors never clear the similarity bar, so this is the
    # keyword half; what the rebuild restored is the *availability* of the
    # vector arm, asserted through the note above).
    hits = client.get("/api/search?q=seeded&k=5").json()["hits"]
    assert any(hit["title"] == "Seeded node 0" for hit in hits)


def test_rebuild_route_refuses_an_unknown_or_unavailable_projector(client, fresh_db):
    """A name outside the registry is the ordinary 400; an unavailable
    projector refuses rather than emptying its store (nothing to refill it)."""
    unknown = client.post("/api/projectors/nope/rebuild")
    assert unknown.status_code == 400, unknown.text
    assert "unknown projector" in unknown.text

    # The autouse fixture pins the provider unavailable, so `vec` cannot be
    # rebuilt: dropping the store without a provider would blind search for
    # good.
    unavailable = client.post("/api/projectors/vec/rebuild")
    assert unavailable.status_code == 400, unavailable.text
    assert "cannot rebuild" in unavailable.text
