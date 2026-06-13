"""Focused CLI↔API parity: identical JSON for search and expand on one seed.

Both surfaces serialise the same ``model_dump(mode="json")`` envelope over the
same rows, so the parsed JSON dicts must compare exactly equal.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient


def _seed(run_cli: Callable[..., dict]) -> tuple[str, str, str]:
    """Create a tiny chain a→b→c and return the three node UUIDs."""
    a = run_cli("add-node", "Postgres stores the graph.", "-t", "claim")["uuid"]
    b = run_cli("add-node", "The graph is queried with SQL.", "-t", "claim")["uuid"]
    c = run_cli("add-node", "SQL is a query language.", "-t", "claim")["uuid"]
    run_cli("add-edge", a, b, "-t", "relates")
    run_cli("add-edge", b, c, "-t", "relates")
    return a, b, c


def test_search_parity(run_cli: Callable[..., dict], client: TestClient) -> None:
    """CLI search JSON equals the API search JSON for the same query."""
    _seed(run_cli)
    cli_result = run_cli("search", "graph")
    api_result = client.get("/search", params={"q": "graph"}).json()
    assert api_result == cli_result


def test_expand_parity(run_cli: Callable[..., dict], client: TestClient) -> None:
    """CLI expand JSON equals the API expand JSON for the same seed and depth."""
    a, _b, _c = _seed(run_cli)
    cli_result = run_cli("expand", a, "-d", "2")
    api_result = client.get("/expand", params={"seed": a, "depth": 2}).json()
    assert api_result == cli_result
