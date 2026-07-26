"""Title-prefix suggestions: the editor's `[[` autocomplete, index-free."""

from __future__ import annotations

import json
import unicodedata

import pytest
from helpers import agent, owner
from typer.testing import CliRunner

from nodum import service
from nodum.cli import app

AGENT = "agent:researcher"
runner = CliRunner()


def _titles(nodes):
    return [node.title for node in nodes]


def _library():
    """Titles that exercise case folding, ranking, and state filtering."""
    return {
        "graph": service.create_node(type="concept", title="Graph Theory", principal=owner()),
        "grammar": service.create_node(type="note", title="grammar notes", principal=owner()),
        "graphite": service.create_node(type="note", title="graphite", principal=owner()),
        "other": service.create_node(type="note", title="Osmosis", principal=owner()),
    }


def test_prefix_match_is_case_insensitive(fresh_db):
    _library()
    assert set(_titles(service.suggest_links("gra", principal=owner()))) == {
        "Graph Theory",
        "grammar notes",
        "graphite",
    }
    assert _titles(service.suggest_links("GRAPH", principal=owner())) == [
        "Graph Theory",
        "graphite",
    ]


def test_typed_case_ranks_first_then_title(fresh_db):
    _library()
    # All three match "Gra" case-insensitively; only "Graph Theory" matches
    # the typed case, so it leads regardless of alphabetical order.
    assert _titles(service.suggest_links("Gra", principal=owner())) == [
        "Graph Theory",
        "grammar notes",
        "graphite",
    ]
    # Typed all-lowercase, the two lowercase titles lead — then by title.
    assert _titles(service.suggest_links("gra", principal=owner())) == [
        "grammar notes",
        "graphite",
        "Graph Theory",
    ]


def test_prefix_only_never_substring(fresh_db):
    _library()
    assert service.suggest_links("theory", principal=owner()) == []
    assert service.suggest_links("mosis", principal=owner()) == []


def test_folds_case_beyond_ascii(fresh_db):
    service.create_node(type="concept", title="École normale", principal=owner())
    service.create_node(type="concept", title="Ökologie", principal=owner())
    assert _titles(service.suggest_links("éc", principal=owner())) == ["École normale"]
    assert _titles(service.suggest_links("ÖK", principal=owner())) == ["Ökologie"]
    # The reason folding happens in Python: SQL `lower()` cannot do this one.
    service.create_node(type="concept", title="Straße", principal=owner())
    assert _titles(service.suggest_links("STRASSE", principal=owner())) == ["Straße"]


def test_matches_across_unicode_normalisation_forms(fresh_db):
    """An accented letter is one code point or two, depending on who typed it.

    NFD titles arrive from macOS paths and some input methods; a browser sends
    NFC. Comparing code points without normalising loses the match entirely.
    """
    decomposed = unicodedata.normalize("NFD", "École normale")
    composed = unicodedata.normalize("NFC", "Cinéma vérité")
    assert decomposed != unicodedata.normalize("NFC", decomposed)  # the forms differ
    service.create_node(type="concept", title=decomposed, principal=owner())
    service.create_node(type="concept", title=composed, principal=owner())

    for form in ("NFC", "NFD"):
        assert _titles(
            service.suggest_links(unicodedata.normalize(form, "École"), principal=owner())
        ) == [decomposed]
        assert _titles(
            service.suggest_links(unicodedata.normalize(form, "Ciné"), principal=owner())
        ) == [composed]
        # …and case-folded on top of the normalisation.
        assert _titles(
            service.suggest_links(unicodedata.normalize(form, "école"), principal=owner())
        ) == [decomposed]


def test_archived_excluded_proposed_included(fresh_db):
    library = _library()
    service.create_node(type="note", title="graph draft", principal=agent(AGENT))  # proposed
    service.transition(library["graphite"].id, "archive", principal=owner())
    titles = _titles(service.suggest_links("graph", principal=owner()))
    assert "graph draft" in titles
    assert "graphite" not in titles


def test_limit_caps_and_empty_prefix_matches_all(fresh_db):
    _library()
    assert len(service.suggest_links("", limit=2, principal=owner())) == 2
    assert len(service.suggest_links("", principal=owner())) == 4
    with pytest.raises(ValueError, match="limit"):
        service.suggest_links("gra", limit=0, principal=owner())


def test_untitled_nodes_are_never_suggested(fresh_db):
    service.create_node(type="note", content="body with no title", principal=owner())
    assert service.suggest_links("", principal=owner()) == []


def test_works_without_any_projector_run(fresh_db):
    """Autocomplete must never be silently empty on a cold database."""
    from nodum import projectors

    library = _library()
    statuses = {status.name: status for status in projectors.projector_status()}
    assert statuses["fts"].last_event_seq == 0  # nothing has been projected
    assert _titles(service.suggest_links("osm", principal=owner())) == [library["other"].title]


def test_deterministic_across_calls(fresh_db):
    for _index in range(6):
        service.create_node(type="note", title="same title", principal=owner())
    first = [node.id for node in service.suggest_links("same", principal=owner())]
    assert first == [node.id for node in service.suggest_links("same", principal=owner())]
    assert len(first) == 6


def test_cli_suggest_links(fresh_db):
    _library()
    result = runner.invoke(app, ["suggest-links", "Gra", "--limit", "2", "--as", "owner"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["count"] == 2
    assert [node["title"] for node in payload["nodes"]] == ["Graph Theory", "grammar notes"]


def test_cli_rejects_bad_limit(fresh_db):
    result = runner.invoke(app, ["suggest-links", "gra", "--limit", "0", "--as", "owner"])
    assert result.exit_code == 1
    assert "limit" in result.stderr
