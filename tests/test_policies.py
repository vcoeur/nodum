"""Agent policies (design §8.3): storage, validation, and write-path evaluation."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from nodum import service
from nodum.cli import app
from nodum.service import PolicyNotFound, TypeNotFound

runner = CliRunner()


def _run_json(*args):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _run_fail(*args):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 1
    return result


#: A gated rule that opts in to grading the agent's *self-reported* confidence.
MENTIONS_RULE = {
    "edge_type": "mentions",
    "min_confidence": 0.9,
    "action": "auto_accept",
    "trust_self_reported_confidence": True,
}

#: The same rule without the opt-in — inert on the direct write path.
UNTRUSTED_MENTIONS_RULE = {
    "edge_type": "mentions",
    "min_confidence": 0.9,
    "action": "auto_accept",
}


def _two_nodes():
    a = service.create_node(type="concept", title="A")
    b = service.create_node(type="concept", title="B")
    return a, b


# ── Policy CRUD ───────────────────────────────────────────────────────────────


def test_set_get_list_policy_roundtrip(fresh_db):
    rules = [MENTIONS_RULE, {"job": "entity_resolution", "action": "always_propose"}]
    policy = service.set_policy("agent:researcher", rules)
    assert policy.agent == "agent:researcher"
    assert policy.rules == rules
    assert policy.updated_by == "human"

    fetched = service.get_policy("agent:researcher")
    assert fetched == policy

    service.set_policy("agent:gardener", [{"job": "link_inference", "action": "auto_apply"}])
    agents = [p.agent for p in service.list_policies()]
    assert agents == ["agent:gardener", "agent:researcher"]


def test_set_policy_replaces_ruleset(fresh_db):
    service.set_policy("agent:researcher", [MENTIONS_RULE])
    updated = service.set_policy("agent:researcher", [], actor="agent:admin")
    assert updated.rules == []
    assert updated.updated_by == "agent:admin"


def test_get_missing_policy_raises(fresh_db):
    with pytest.raises(PolicyNotFound):
        service.get_policy("agent:nobody")


def test_set_policy_validates_action(fresh_db):
    with pytest.raises(ValueError, match="action"):
        service.set_policy("agent:x", [{"edge_type": "mentions", "action": "yolo"}])


def test_set_policy_requires_a_key(fresh_db):
    with pytest.raises(ValueError, match="job.*edge_type"):
        service.set_policy("agent:x", [{"action": "auto_accept"}])


def test_set_policy_validates_min_confidence(fresh_db):
    with pytest.raises(ValueError, match="min_confidence"):
        service.set_policy(
            "agent:x", [{"edge_type": "mentions", "min_confidence": 1.5, "action": "auto_accept"}]
        )


def test_set_policy_validates_the_trust_flag(fresh_db):
    with pytest.raises(ValueError, match="trust_self_reported_confidence"):
        service.set_policy(
            "agent:x",
            [
                {
                    "edge_type": "mentions",
                    "action": "auto_accept",
                    "trust_self_reported_confidence": "yes",
                }
            ],
        )


def test_set_policy_resolves_edge_type(fresh_db):
    with pytest.raises(TypeNotFound):
        service.set_policy("agent:x", [{"edge_type": "no-such-edge", "action": "auto_accept"}])


def test_set_policy_preserves_extra_keys(fresh_db):
    rule = {"job": "link_inference", "action": "auto_apply", "note": "tune later"}
    policy = service.set_policy("agent:x", [rule])
    assert policy.rules[0]["note"] == "tune later"


def test_policy_set_emits_audited_event(fresh_db):
    service.set_policy("agent:researcher", [MENTIONS_RULE])
    service.set_policy("agent:researcher", [])
    events = service.list_events(limit=2)
    assert [e.op for e in events] == ["policy.set", "policy.set"]
    latest, first = events
    assert latest.actor == "human"
    assert latest.payload["agent"] == "agent:researcher"
    assert latest.payload["before"] == [MENTIONS_RULE]
    assert latest.payload["after"] == []
    assert first.payload["before"] is None


# ── Write-path evaluation ─────────────────────────────────────────────────────


def test_auto_accept_matching_rule_and_confidence(fresh_db):
    a, b = _two_nodes()
    service.set_policy("agent:researcher", [MENTIONS_RULE])
    edge = service.create_edge(a.id, b.id, "mentions", confidence=0.95, actor="agent:researcher")
    assert edge.state == "active"
    assert edge.created_by == "agent:researcher"

    events = service.list_events(limit=1)
    assert events[0].op == "edge.create"  # op records the landing state
    assert events[0].actor == "agent:researcher"
    assert events[0].payload["policy_rule"] == MENTIONS_RULE


# ── Self-reported confidence is untrusted input ───────────────────────────────


def test_self_reported_confidence_alone_never_auto_accepts(fresh_db):
    """The agent picks the number it is graded on — the gate needs an opt-in."""
    a, b = _two_nodes()
    service.set_policy("agent:researcher", [UNTRUSTED_MENTIONS_RULE])
    for claimed in (0.9, 0.95, 1.0):
        edge = service.create_edge(
            a.id, b.id, "mentions", confidence=claimed, actor="agent:researcher"
        )
        assert edge.state == "proposed"
        assert "policy_rule" not in service.list_events(limit=1)[0].payload


def test_batch_proposals_cannot_buy_auto_accept_with_confidence(fresh_db):
    a, b = _two_nodes()
    service.set_policy("agent:researcher", [UNTRUSTED_MENTIONS_RULE])
    result = service.propose_edges(
        [{"src": a.id, "dst": b.id, "edge_type": "mentions", "confidence": 1.0}],
        actor="agent:researcher",
    )
    assert result.created[0].state == "proposed"


def test_trust_flag_opts_the_gate_back_in(fresh_db):
    a, b = _two_nodes()
    service.set_policy("agent:researcher", [MENTIONS_RULE])
    trusted = service.create_edge(a.id, b.id, "mentions", confidence=0.95, actor="agent:researcher")
    assert trusted.state == "active"


def test_trust_flag_does_not_bypass_the_gate_itself(fresh_db):
    a, b = _two_nodes()
    service.set_policy("agent:researcher", [MENTIONS_RULE])
    edge = service.create_edge(a.id, b.id, "mentions", confidence=0.5, actor="agent:researcher")
    assert edge.state == "proposed"


def test_trust_flag_without_a_gate_is_unremarkable(fresh_db):
    a, b = _two_nodes()
    service.set_policy(
        "agent:researcher",
        [
            {
                "edge_type": "mentions",
                "action": "auto_accept",
                "trust_self_reported_confidence": False,
            }
        ],
    )
    # No gate to grade, so the rule is the human's plain unconditional grant.
    edge = service.create_edge(a.id, b.id, "mentions", confidence=0.0, actor="agent:researcher")
    assert edge.state == "active"


def test_below_threshold_stays_proposed(fresh_db):
    a, b = _two_nodes()
    service.set_policy("agent:researcher", [MENTIONS_RULE])
    edge = service.create_edge(a.id, b.id, "mentions", confidence=0.5, actor="agent:researcher")
    assert edge.state == "proposed"
    events = service.list_events(limit=1)
    assert events[0].op == "edge.propose"
    assert "policy_rule" not in events[0].payload


def test_missing_confidence_fails_gate(fresh_db):
    a, b = _two_nodes()
    service.set_policy("agent:researcher", [MENTIONS_RULE])
    edge = service.create_edge(a.id, b.id, "mentions", actor="agent:researcher")
    assert edge.state == "proposed"


def test_rule_without_gate_auto_accepts(fresh_db):
    a, b = _two_nodes()
    service.set_policy("agent:researcher", [{"edge_type": "mentions", "action": "auto_accept"}])
    edge = service.create_edge(a.id, b.id, "mentions", actor="agent:researcher")
    assert edge.state == "active"


def test_no_policy_stays_proposed(fresh_db):
    a, b = _two_nodes()
    edge = service.create_edge(a.id, b.id, "mentions", confidence=1.0, actor="agent:researcher")
    assert edge.state == "proposed"


def test_policy_is_per_actor(fresh_db):
    a, b = _two_nodes()
    service.set_policy("agent:researcher", [MENTIONS_RULE])
    edge = service.create_edge(a.id, b.id, "mentions", confidence=1.0, actor="agent:other")
    assert edge.state == "proposed"


def test_edge_type_must_match(fresh_db):
    a, b = _two_nodes()
    service.set_policy("agent:researcher", [MENTIONS_RULE])
    edge = service.create_edge(a.id, b.id, "supports", confidence=1.0, actor="agent:researcher")
    assert edge.state == "proposed"


def test_non_auto_accept_actions_stay_proposed(fresh_db):
    a, b = _two_nodes()
    service.set_policy(
        "agent:researcher",
        [
            {"edge_type": "mentions", "action": "auto_apply", "min_confidence": 0.1},
            {"edge_type": "supports", "action": "always_propose"},
        ],
    )
    for edge_type in ("mentions", "supports"):
        edge = service.create_edge(a.id, b.id, edge_type, confidence=1.0, actor="agent:researcher")
        assert edge.state == "proposed"


def test_job_rules_do_not_affect_writes(fresh_db):
    a, b = _two_nodes()
    service.set_policy("agent:researcher", [{"job": "link_inference", "action": "auto_apply"}])
    edge = service.create_edge(a.id, b.id, "mentions", confidence=1.0, actor="agent:researcher")
    assert edge.state == "proposed"


def test_human_writes_ignore_policies(fresh_db):
    a, b = _two_nodes()
    service.set_policy("human", [MENTIONS_RULE])
    edge = service.create_edge(a.id, b.id, "mentions", confidence=0.1)
    assert edge.state == "active"
    events = service.list_events(limit=1)
    assert "policy_rule" not in events[0].payload


def test_node_writes_unaffected_by_edge_policy(fresh_db):
    service.set_policy("agent:researcher", [MENTIONS_RULE])
    node = service.create_node(type="note", title="bot", actor="agent:researcher")
    assert node.state == "proposed"


def test_auto_accepted_edge_is_undoable(fresh_db):
    a, b = _two_nodes()
    service.set_policy("agent:researcher", [MENTIONS_RULE])
    service.create_edge(a.id, b.id, "mentions", confidence=0.95, actor="agent:researcher")
    result = service.undo()
    assert result.undone_op == "edge.create"
    assert service.list_edges(node_id=a.id) == []


def test_undo_default_skips_policy_events(fresh_db):
    service.create_node(type="note", title="target")
    service.set_policy("agent:researcher", [MENTIONS_RULE])
    result = service.undo()  # latest undoable event is the node create, not policy.set
    assert result.undone_op == "node.create"


def test_undo_explicit_policy_event_refused(fresh_db):
    service.set_policy("agent:researcher", [MENTIONS_RULE])
    policy_seq = service.list_events(limit=1)[0].seq
    with pytest.raises(ValueError, match="not a graph event"):
        service.undo(policy_seq)


# ── CLI ───────────────────────────────────────────────────────────────────────


def test_cli_policy_set_get_list(fresh_db):
    policy = _run_json("policy", "set", "agent:researcher", "--rule", json.dumps(MENTIONS_RULE))
    assert policy["agent"] == "agent:researcher"
    assert policy["rules"] == [MENTIONS_RULE]

    fetched = _run_json("policy", "get", "agent:researcher")
    assert fetched == policy

    listing = _run_json("policy", "list")
    assert listing["count"] == 1

    missing = _run_fail("policy", "get", "agent:nobody")
    assert "no policy" in missing.output


def test_cli_policy_set_rejects_bad_rule_json(fresh_db):
    result = _run_fail("policy", "set", "agent:x", "--rule", "not-json")
    assert "--rule expects a JSON object" in result.output


def test_cli_policy_set_rejects_invalid_rule(fresh_db):
    result = _run_fail(
        "policy", "set", "agent:x", "--rule", json.dumps({"action": "nope", "job": "j"})
    )
    assert "action" in result.output


def test_cli_auto_accept_end_to_end(fresh_db):
    a = _run_json("node", "create", "--type", "concept", "--title", "A")
    b = _run_json("node", "create", "--type", "concept", "--title", "B")
    _run_json("policy", "set", "agent:researcher", "--rule", json.dumps(MENTIONS_RULE))
    edge = _run_json(
        "edge",
        "create",
        a["id"],
        b["id"],
        "--type",
        "mentions",
        "--confidence",
        "0.95",
        "--actor",
        "agent:researcher",
    )
    assert edge["state"] == "active"
