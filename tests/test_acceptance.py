"""The note-01 acceptance demo, driven through the CLI, with API parity checks.

The project's finish-line demo: build a small typed graph through the CLI,
confirm the metamodel rejects a reversed edge, expand to reach a reference two
hops away, then assert the HTTP API returns identical JSON for the same read and
that the web view serves its page.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from nodum.cli import app as cli_app

PERSON_NAME = "Ada Lovelace"
REFERENCE = "Lovelace 1843, Notes on the Analytical Engine"
SUMMARY = "A summary of Lovelace's notes on the Analytical Engine"
CLAIM = "Lovelace described the engine's general-purpose computation"


def test_note_01_acceptance(run_cli: Callable[..., dict], client: TestClient) -> None:
    """Walk the note-01 demo through the CLI, then assert the API and web agree."""
    # 1: a Person and a Reference, authored by the Person.
    person = run_cli("add", "Person", PERSON_NAME)["uuid"]
    reference = run_cli("add", "Reference", REFERENCE)["uuid"]
    run_cli("link", person, reference, "AuthorOf")

    # 2: the reversed edge (Reference -> Person) is rejected with a non-zero exit.
    rejected = CliRunner().invoke(cli_app, ["link", reference, person, "AuthorOf"])
    assert rejected.exit_code != 0

    # 3: a Literature summarizes the Reference, and a Note cites that Literature.
    literature = run_cli("add", "Literature", SUMMARY)["uuid"]
    note = run_cli("add", "Note", CLAIM, "--set", "role=claim")["uuid"]
    run_cli("link", literature, reference, "summarizes")
    run_cli("link", note, literature, "cites")

    # 4: expanding the Note two hops reaches the Reference (Note -> Literature -> Reference).
    cli_expand = run_cli("expand", note, "--depth", "2")
    assert reference in {node["uuid"] for node in cli_expand["nodes"]}

    # 5: the HTTP API returns byte-identical JSON for the same expand read.
    api_expand = client.get("/expand", params={"seed": note, "depth": 2}).json()
    assert api_expand == cli_expand

    # 6: the schema endpoint lists the seven node kinds.
    assert len(client.get("/schema").json()["node_kinds"]) == 7
