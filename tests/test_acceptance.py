"""The note-01 acceptance demo, driven through the CLI, with API parity checks.

This is the project's finish-line demo: build a tiny claim graph through the
CLI, exercise search and expand, then assert the HTTP API returns byte-identical
JSON for the same reads and that the web view serves its page.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient

CLAIM_ONE = "Ada Lovelace wrote the first published algorithm."
CLAIM_TWO = "The Analytical Engine was designed by Charles Babbage."
CLAIM_THREE = "Ada Lovelace and Charles Babbage collaborated on the Analytical Engine."


def test_note_01_demo(run_cli: Callable[..., dict], client: TestClient) -> None:
    """Walk the full note-01 demo through the CLI, then assert the API agrees."""
    # 1-3: three claim nodes.
    n1 = run_cli("add-node", CLAIM_ONE, "-t", "claim")["uuid"]
    n2 = run_cli("add-node", CLAIM_TWO, "-t", "claim")["uuid"]
    n3 = run_cli("add-node", CLAIM_THREE, "-t", "claim")["uuid"]

    # 4: n3 supports both n1 and n2.
    run_cli("add-edge", n3, n1, "-t", "supports")
    run_cli("add-edge", n3, n2, "-t", "supports")

    # 5: search finds the Analytical-Engine claims (n2, n3) but not n1.
    search = run_cli("search", "Analytical Engine")
    hit_uuids = {hit["uuid"] for hit in search["hits"]}
    assert n2 in hit_uuids
    assert n3 in hit_uuids
    assert n1 not in hit_uuids

    # 6: expanding n3 reaches n1 and n2 over `supports` edges.
    expand = run_cli("expand", n3, "-d", "1")
    node_uuids = {node["uuid"] for node in expand["nodes"]}
    assert {n1, n2, n3} <= node_uuids
    edge_triples = {(e["from_uuid"], e["to_uuid"], e["data"].get("type")) for e in expand["edges"]}
    assert (n3, n1, "supports") in edge_triples
    assert (n3, n2, "supports") in edge_triples

    # 7: the HTTP API returns identical JSON for the same reads.
    api_search = client.get("/search", params={"q": "Analytical Engine"}).json()
    assert api_search == search
    api_expand = client.get("/expand", params={"seed": n3, "depth": 1}).json()
    assert api_expand == expand

    # 8: the web view serves a non-empty HTML page.
    home = client.get("/")
    assert home.status_code == 200
    assert "text/html" in home.headers["content-type"]
    assert home.text.strip()
    assert "search" in home.text.lower()
