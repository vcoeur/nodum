"""Capability URLs: minting, single use, expiry, scope, and the audit trail.

A token is a secret whose sha-256 is stored, so most of what matters here is
what the *database* does not contain and what a second attempt gets. The clock
is never slept on: an expiry is backdated in SQL, which is also the only clock
this module consults.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from helpers import agent, owner, seed_space

from nodum import assets, auth, db, service, urls
from nodum.assets import AssetNotFound
from nodum.store import GrantNotPermitted
from nodum.urls import TokenInvalid

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< >>\nendobj\n"


def _register(tmp_path, name="report.pdf", data=PDF_BYTES):
    """Register a small file as an asset and return its AssetOut."""
    source = tmp_path / name
    source.write_bytes(data)
    return assets.register_asset(source)


def _describe(asset, *, space=None, principal=None):
    """Create the ``asset_ref`` node that makes an asset reachable in a space."""
    return service.create_node(
        type="asset_ref",
        title=asset.original_name,
        space=space,
        props={"asset_hash": asset.hash},
        principal=principal or owner(),
    )


def _rows():
    """Every ``url_tokens`` row, as plain dicts, oldest first."""
    conn = db.connect()
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM url_tokens ORDER BY rowid")]
    finally:
        conn.close()


def _count(table):
    conn = db.connect()
    try:
        return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
    finally:
        conn.close()


def _now():
    conn = db.connect()
    try:
        return conn.execute("SELECT datetime('now')").fetchone()[0]
    finally:
        conn.close()


def _asset_events():
    """The ``asset.*`` entries in the log, oldest first."""
    events = service.list_events(owner(), limit=200)
    return [event for event in reversed(events) if event.op.startswith("asset.")]


def _expire(token):
    """Backdate one token's expiry — the clock is never slept on."""
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE url_tokens SET expires_at = datetime('now', '-1 day') WHERE token_hash = ?",
            (hashlib.sha256(token.encode()).hexdigest(),),
        )
        conn.commit()
    finally:
        conn.close()


# ── Minting a download grant ──────────────────────────────────────────────────


def test_a_download_grant_carries_the_url_the_route_will_serve(fresh_db, tmp_path):
    asset = _register(tmp_path)

    grant = urls.mint_download(asset.hash, principal=owner(), base_url="http://host:9000/")

    assert grant.kind == "download"
    assert grant.asset_hash == asset.hash
    assert grant.url == f"http://host:9000/api/download/{grant.token}"
    assert grant.max_bytes is None
    assert grant.expires_at > _now()


def test_the_url_defaults_to_the_public_base_url(fresh_db, tmp_path, monkeypatch):
    asset = _register(tmp_path)
    monkeypatch.delenv(urls.PUBLIC_URL_ENV, raising=False)
    assert urls.public_base_url() == urls.DEFAULT_PUBLIC_URL

    grant = urls.mint_download(asset.hash, principal=owner())
    assert grant.url == f"{urls.DEFAULT_PUBLIC_URL}/api/download/{grant.token}"

    monkeypatch.setenv(urls.PUBLIC_URL_ENV, "https://graph.example/")
    assert urls.public_base_url() == "https://graph.example"


def test_a_download_grant_resolves_a_describing_node_id_too(fresh_db, tmp_path):
    asset = _register(tmp_path)
    node = _describe(asset)

    assert urls.mint_download(node.id, principal=owner()).asset_hash == asset.hash


# ── The secret never lands in the database ────────────────────────────────────


def test_the_stored_row_never_holds_the_secret(fresh_db, tmp_path):
    """The row is a hash and some metadata — a read leak hands out no URL."""
    asset = _register(tmp_path)
    grant = urls.mint_download(asset.hash, principal=owner())

    (row,) = _rows()
    assert grant.token not in json.dumps(row)
    assert row["token_hash"] == hashlib.sha256(grant.token.encode()).hexdigest()
    assert row["used_at"] is None
    assert row["created_by"] == "human:owner"


def test_the_token_is_generated_the_way_agent_tokens_are(fresh_db, tmp_path):
    """One generator, one prefix: a log scrub for `ndm_` finds every credential."""
    asset = _register(tmp_path)

    token = urls.mint_download(asset.hash, principal=owner()).token

    assert token.startswith(auth.TOKEN_PREFIX)
    assert len(token) > 40  # 32 random bytes, url-safe


# ── Single use ────────────────────────────────────────────────────────────────


def test_a_token_is_spent_by_its_first_redemption(fresh_db, tmp_path):
    asset = _register(tmp_path)
    grant = urls.mint_download(asset.hash, principal=owner())

    row = urls.consume(grant.token, kind="download")
    assert row["asset_hash"] == asset.hash
    assert row["used_at"] is not None

    with pytest.raises(TokenInvalid):
        urls.consume(grant.token, kind="download")


def test_an_expired_token_is_refused_and_stays_unspent(fresh_db, tmp_path):
    asset = _register(tmp_path)
    grant = urls.mint_download(asset.hash, principal=owner())
    _expire(grant.token)

    with pytest.raises(TokenInvalid):
        urls.consume(grant.token, kind="download")
    assert _rows()[0]["used_at"] is None


def test_the_wrong_kind_is_refused_without_burning_the_token(fresh_db, tmp_path):
    """A stray request at the other route must not spend somebody's grant."""
    asset = _register(tmp_path)
    grant = urls.mint_download(asset.hash, principal=owner())

    with pytest.raises(TokenInvalid):
        urls.consume(grant.token, kind="upload")

    assert urls.consume(grant.token, kind="download")["kind"] == "download"


def test_an_unknown_kind_is_a_programming_error_not_a_token_error(fresh_db):
    with pytest.raises(ValueError, match="kind must be"):
        urls.consume("ndm_whatever", kind="sideways")


@pytest.mark.parametrize("case", ["unknown", "garbage", "expired", "spent", "wrong kind"])
def test_every_refusal_reads_identically(fresh_db, tmp_path, case):
    """ "Expired" says a token once existed; "wrong kind" says which route to try."""
    asset = _register(tmp_path)
    grant = urls.mint_download(asset.hash, principal=owner())
    token, kind = grant.token, "download"
    if case == "unknown":
        token = auth.TOKEN_PREFIX + "a" * 43
    elif case == "garbage":
        token = "not a token at all"
    elif case == "expired":
        _expire(token)
    elif case == "spent":
        urls.consume(token, kind="download")
    else:
        kind = "upload"

    with pytest.raises(TokenInvalid) as refusal:
        urls.consume(token, kind=kind)
    assert str(refusal.value) == urls.INVALID_TOKEN_MESSAGE


# ── Scope: a download grant is as reachable as the asset ──────────────────────


def test_mint_download_refuses_an_asset_the_agent_cannot_reach(fresh_db, tmp_path):
    """The describing node carries the space, so it carries the isolation too."""
    asset = _register(tmp_path)
    seed_space("b")
    _describe(asset, space="b")
    outsider = agent("outsider", grants={"main": "read"})

    with pytest.raises(AssetNotFound):
        urls.mint_download(asset.hash, principal=outsider)
    # Refused means refused: no capability row, and nothing in the log either.
    assert _rows() == []
    assert _asset_events() == []


def test_mint_download_refuses_bytes_nobody_describes(fresh_db, tmp_path):
    """Undescribed bytes are a human's business until ingestion has run."""
    asset = _register(tmp_path)
    reader = agent("reader", grants={"main": "read"})

    with pytest.raises(AssetNotFound):
        urls.mint_download(asset.hash, principal=reader)
    assert urls.mint_download(asset.hash, principal=owner()).asset_hash == asset.hash


def test_an_agent_reaching_the_asset_gets_its_grant(fresh_db, tmp_path):
    asset = _register(tmp_path)
    seed_space("b")
    _describe(asset, space="b")
    insider = agent("insider", grants={"b": "read"})

    grant = urls.mint_download(asset.hash, principal=insider)

    assert grant.asset_hash == asset.hash
    assert _rows()[0]["created_by"] == "agent:insider"


# ── Upload grants ─────────────────────────────────────────────────────────────


def test_an_upload_grant_carries_its_ceiling_and_target(fresh_db):
    result = urls.mint_upload("scan.pdf", "application/pdf", 4096, principal=owner())

    assert result.asset is None
    assert result.grant.kind == "upload"
    assert result.grant.max_bytes == 4096
    assert result.grant.asset_hash is None
    assert result.grant.url.endswith(f"/api/uploads/{result.grant.token}")

    (row,) = _rows()
    assert (row["original_name"], row["mime"], row["max_bytes"]) == (
        "scan.pdf",
        "application/pdf",
        4096,
    )
    assert row["space_id"] == "main"


def test_an_upload_grant_targets_the_named_space(fresh_db):
    seed_space("b")
    writer = agent("writer", grants={"meta": "read", "b": "edit"})

    urls.mint_upload("scan.pdf", "application/pdf", 10, space="b", principal=writer)

    assert _rows()[0]["space_id"] == "b"


def test_an_upload_grant_needs_a_writable_target_space(fresh_db):
    """A grant whose describing node can never be written is a wasted upload."""
    seed_space("b")
    reader = agent("reader", grants={"meta": "read", "b": "read"})

    with pytest.raises(GrantNotPermitted):
        urls.mint_upload("scan.pdf", "application/pdf", 10, space="b", principal=reader)
    assert _rows() == []


def test_an_unreachable_space_looks_exactly_like_a_missing_one(fresh_db):
    seed_space("b")
    writer = agent("writer", grants={"meta": "read", "main": "edit"})

    with pytest.raises(service.TypeNotFound, match="unknown space: b"):
        urls.mint_upload("scan.pdf", "application/pdf", 10, space="b", principal=writer)


def test_a_known_hash_returns_the_asset_and_moves_no_bytes(fresh_db, tmp_path):
    """Design §5.7 rule 4: the declared hash is what makes the dedup instant."""
    asset = _register(tmp_path)
    blobs_before = _count("asset_blobs")

    result = urls.mint_upload(
        "report.pdf", "application/pdf", len(PDF_BYTES), sha256=asset.hash, principal=owner()
    )

    assert result.grant is None
    assert result.asset.hash == asset.hash
    assert _rows() == []  # no capability was handed out
    assert _count("asset_blobs") == blobs_before


def test_the_dedup_hit_is_logged_like_any_other_grant(fresh_db, tmp_path):
    asset = _register(tmp_path)

    urls.mint_upload("report.pdf", "application/pdf", 12, sha256=asset.hash, principal=owner())

    (event,) = _asset_events()
    assert event.op == "asset.upload_url"
    assert event.payload["dedup"] is True
    assert event.payload["asset_hash"] == asset.hash
    assert event.payload["token_id"] is None


def test_the_dedup_shortcut_is_scoped_like_every_other_asset_read(fresh_db, tmp_path):
    """Answering "that already exists" for an unreachable hash is an oracle.

    The agent gets an ordinary grant instead, and the bytes converge anyway —
    ``register_asset`` dedups them on arrival.
    """
    asset = _register(tmp_path)
    seed_space("b")
    _describe(asset, space="b")
    outsider = agent("outsider", grants={"meta": "read", "main": "edit"})

    result = urls.mint_upload(
        "report.pdf", "application/pdf", 12, sha256=asset.hash, principal=outsider
    )

    assert result.asset is None
    assert result.grant is not None
    assert _rows()[0]["asset_hash"] == asset.hash  # declared, not resolved


def test_an_upload_token_is_also_single_use(fresh_db):
    grant = urls.mint_upload("scan.pdf", "application/pdf", 10, principal=owner()).grant

    assert urls.consume(grant.token, kind="upload")["original_name"] == "scan.pdf"
    with pytest.raises(TokenInvalid):
        urls.consume(grant.token, kind="upload")


# ── Bounds ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("size", [urls.MAX_UPLOAD_BYTES + 1, -1])
def test_a_size_outside_the_cap_is_refused(fresh_db, size):
    with pytest.raises(ValueError, match="size must be between"):
        urls.mint_upload("huge.bin", "application/octet-stream", size, principal=owner())
    assert _rows() == []


def test_the_cap_is_exactly_reachable(fresh_db):
    grant = urls.mint_upload(
        "big.pdf", "application/pdf", urls.MAX_UPLOAD_BYTES, principal=owner()
    ).grant

    assert grant.max_bytes == urls.MAX_UPLOAD_BYTES


@pytest.mark.parametrize("ttl", [0, -1, urls.MAX_TTL_SECONDS + 1])
def test_a_ttl_outside_its_bounds_is_refused(fresh_db, tmp_path, ttl):
    """The parameter must not be a way to mint a permanent capability."""
    asset = _register(tmp_path)

    with pytest.raises(ValueError, match="ttl_seconds"):
        urls.mint_download(asset.hash, principal=owner(), ttl_seconds=ttl)
    with pytest.raises(ValueError, match="ttl_seconds"):
        urls.mint_upload("x.pdf", "application/pdf", 1, principal=owner(), ttl_seconds=ttl)
    assert _rows() == []


def test_the_default_ttl_is_minutes_not_hours(fresh_db):
    assert 0 < urls.DEFAULT_TTL_SECONDS <= 900
    assert urls.MAX_TTL_SECONDS <= 3600


@pytest.mark.parametrize("declared", ["not-a-hash", "AB" * 32, "abc"])
def test_a_malformed_declared_hash_is_refused(fresh_db, declared):
    with pytest.raises(ValueError, match="sha256"):
        urls.mint_upload("x.pdf", "application/pdf", 1, sha256=declared, principal=owner())


# ── The audit trail ───────────────────────────────────────────────────────────


def test_every_mint_and_redemption_appears_in_the_log(fresh_db, tmp_path):
    asset = _register(tmp_path)
    download = urls.mint_download(asset.hash, principal=owner())
    upload = urls.mint_upload("scan.pdf", "application/pdf", 10, principal=owner()).grant
    urls.consume(download.token, kind="download")
    urls.consume(upload.token, kind="upload")

    assert [event.op for event in _asset_events()] == [
        "asset.download_url",
        "asset.upload_url",
        "asset.download",
        "asset.upload",
    ]


def test_no_payload_ever_carries_the_secret_or_its_hash(fresh_db, tmp_path):
    asset = _register(tmp_path)
    download = urls.mint_download(asset.hash, principal=owner())
    upload = urls.mint_upload("scan.pdf", "application/pdf", 10, principal=owner()).grant
    urls.consume(download.token, kind="download")
    urls.consume(upload.token, kind="upload")

    logged = json.dumps([event.payload for event in _asset_events()])
    for secret in (download.token, upload.token):
        assert secret not in logged
        assert hashlib.sha256(secret.encode()).hexdigest() not in logged
    # What a payload does name is the token's public id.
    assert {event.payload["token_id"] for event in _asset_events()} == {
        row["id"] for row in _rows()
    }


def test_a_redemption_is_attributed_to_whoever_minted_it(fresh_db, tmp_path):
    """A capability URL carries no ambient credential — `created_by` is the actor."""
    asset = _register(tmp_path)
    _describe(asset)
    reader = agent("reader", grants={"meta": "read", "main": "read"})
    grant = urls.mint_download(asset.hash, principal=reader)

    urls.consume(grant.token, kind="download")

    assert [event.actor for event in _asset_events()] == ["agent:reader", "agent:reader"]


def test_the_ops_this_module_writes_are_inside_the_service_allowlist(fresh_db):
    """`consume` appends its event directly; the allowlist still has to cover it."""
    written = set(urls.MINT_OPS.values()) | set(urls.REDEEM_OPS.values())

    assert written <= set(service.ASSET_EVENT_OPS)


def test_record_asset_event_refuses_anything_outside_the_allowlist(fresh_db):
    for op in ("node.create", "undo", "asset.whatever", "grant.set"):
        with pytest.raises(ValueError, match="op must be one of"):
            service.record_asset_event(op, {}, principal=owner())

    assert service.record_asset_event("asset.ingest", {"note": "ok"}, principal=owner()) > 0


def test_asset_events_are_audit_only_because_undo_refuses_them(fresh_db, tmp_path):
    """The claim `record_asset_event` makes, asserted rather than restated."""
    asset = _register(tmp_path)
    node = _describe(asset)
    urls.mint_download(asset.hash, principal=owner())
    (event,) = _asset_events()

    with pytest.raises(ValueError, match="not a graph event"):
        service.undo(event.seq, principal=owner())
    # An unqualified undo skips it entirely and lands on the node create.
    assert service.undo(principal=owner()).undone_op == "node.create"
    with pytest.raises(service.NodeNotFound):
        service.get_node(node.id, principal=owner())


# ── Housekeeping ──────────────────────────────────────────────────────────────


def test_minting_sweeps_rows_nobody_came_back_for(fresh_db, tmp_path):
    """Expiry is otherwise only noticed when a dead URL is presented."""
    asset = _register(tmp_path)
    stale = urls.mint_download(asset.hash, principal=owner())
    _expire(stale.token)

    urls.mint_download(asset.hash, principal=owner())

    assert len(_rows()) == 1
    with pytest.raises(TokenInvalid):
        urls.consume(stale.token, kind="download")
