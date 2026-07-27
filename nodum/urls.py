"""Short-lived, single-use capability URLs (design §5.7 rule 4, Phase 4 note 01 D4).

Two escape hatches exist for agent hosts that share no filesystem with the
graph: :func:`mint_download` hands out a URL for an asset's original, and
:func:`mint_upload` hands out a place to PUT bytes exactly once. They are
escape hatches, so both ends of both of them are event-logged.

**A token is a capability, not a signature.** It is 256 bits from
:mod:`secrets` and only its sha-256 reaches the database — the same generator
and the same at-rest treatment as an agent token (:mod:`nodum.auth`), for the
same reason: a database read leak must not hand out live credentials. The
alternative, an HMAC-signed URL, moves the authority into a *key* that has to
be generated, stored, rotated, and kept out of every backup and log — and it
still needs a table the moment anyone wants a URL spent or revoked, because a
signature is valid until it expires and not one moment less. Here the database
is the only state: the row *is* the authority, expiry and single use and
revocation are all one ``UPDATE`` on it, and there is no signing key to
manage, rotate, or leak at all.

Single use is enforced by **rowcount, not by reading first**: redemption is
one ``UPDATE … WHERE used_at IS NULL AND expires_at > datetime('now')`` and
the token is spent iff that statement matched a row. Two concurrent
redemptions of the same URL therefore cannot both succeed, which a
read-then-write would happily allow.

**No Python clock is involved anywhere in this module.** Every timestamp is
SQLite's ``datetime('now')`` — UTC, computed in the database and compared in
the database. The stored strings carry no zone marker (the same
zone-less-timestamp trap the web UI's ``parseTimestamp`` exists for), so a
naive ``datetime.now()`` comparison would quietly honour expired tokens for
the length of the host's UTC offset — and pass every test run in UTC.

**Scope.** :func:`mint_download` resolves its asset through
:func:`nodum.assets.get_asset`, which is already scoped by the
describing-node rule (note 01 D1): an asset the principal cannot read answers
*not found* and no token is minted. :func:`mint_upload`'s dedup shortcut is
scoped through the same call — a declared hash the principal cannot reach is
treated as unknown and gets an ordinary grant, because answering "that
already exists" for anything else would turn the endpoint into an existence
oracle over every byte in the file. The bytes converge anyway:
``register_asset`` dedups on arrival.

The events written here (``asset.download_url``, ``asset.upload_url``,
``asset.download``, ``asset.upload``) are audit records and nothing more —
:func:`nodum.service.undo` reverses ``node.*`` / ``edge.*`` events only and
refuses everything else by name. **No payload ever carries bytes or the
secret**: a payload names the token's *id*, which is public by design.
"""

from __future__ import annotations

import os
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from nodum import assets, auth, db, service
from nodum.models import UploadGrantOut, UrlGrantOut
from nodum.principal import Principal
from nodum.store import Store

#: How long a freshly minted URL lives, in seconds.
#:
#: Five minutes: the hatch exists because a host wants the bytes *now*, so the
#: only latency it has to cover is the round trip from "the tool returned a
#: URL" to "the request reached this server", plus enough slack that a clock a
#: little out of step does not turn a valid grant into a mystery. Anything
#: measured in hours is a URL that outlives the conversation that needed it and
#: sits in a shell history, a proxy log, or an agent transcript being a live
#: credential. Note the TTL is checked when the request *starts*, so a slow
#: transfer is never cut off by it.
DEFAULT_TTL_SECONDS = 300

#: Ceiling on a caller-supplied ``ttl_seconds`` — one hour. Without it the
#: parameter is a way to turn a deliberately short-lived capability into a
#: permanent one, which is the property this whole module is built around.
MAX_TTL_SECONDS = 3600

#: Largest upload a grant may promise (32 MiB).
#:
#: Deliberately equal to ``nodum.http_api.MAX_REQUEST_BYTES``, and it must
#: never exceed it: the upload route sits outside the session gate but still
#: inside the body ceiling (note 02), so a grant promising more than the server
#: is willing to read would be a grant that fails halfway through the
#: transfer. The value is duplicated rather than imported because a domain
#: module has no business importing an adapter.
MAX_UPLOAD_BYTES = 32 * 1024 * 1024

#: Environment variable naming the address clients reach this server on, for
#: the URLs minted here. ``nodum serve`` binds loopback by default, which is
#: also what an agent host running beside it will use.
PUBLIC_URL_ENV = "NODUM_PUBLIC_URL"

#: Fallback for :data:`PUBLIC_URL_ENV`: ``nodum serve``'s own default bind.
DEFAULT_PUBLIC_URL = "http://127.0.0.1:8600"

#: The two token kinds and the URL path each is redeemed at. The routes are the
#: HTTP adapter's job; the paths are fixed here so the minted URL and the route
#: cannot drift apart.
TOKEN_PATHS = {"download": "/api/download", "upload": "/api/uploads"}

#: Event op per kind for a *mint* — the grant was handed out.
MINT_OPS = {"download": "asset.download_url", "upload": "asset.upload_url"}

#: Event op per kind for a *redemption* — the grant was spent. Both maps are
#: subsets of :data:`nodum.service.ASSET_EVENT_OPS`, which is what keeps this
#: module inside the allowlist rather than beside it.
REDEEM_OPS = {"download": "asset.download", "upload": "asset.upload"}

#: What a declared sha256 has to look like before it is stored or looked up:
#: lowercase hex, 64 characters, which is what :mod:`hashlib` produces and what
#: ``assets.hash`` holds. A malformed declaration can never match an asset and
#: can never be satisfied by an upload, so it is refused at the mint rather
#: than after the bytes have crossed the network.
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class PayloadTooLarge(ValueError):
    """Raised when a body — promised or delivered — is more than will be read.

    Both ends of that ceiling live in this module: :data:`MAX_UPLOAD_BYTES` is
    the most a grant may promise, and the HTTP adapter's own
    ``MAX_REQUEST_BYTES`` is deliberately equal to it. The class therefore lives
    here rather than in the adapter, which imports it and maps it to **413** —
    the same direction :class:`TokenInvalid` already runs in, and the only one
    available, since a domain module must not import an adapter.

    A ``ValueError``, so the CLI's ``_run`` reports it as one readable line like
    every other caller mistake; the adapter's explicit 413 row wins over the
    400 it would inherit through ``ValueError``, because status lookup walks the
    MRO. The adapter raises it *mid-read* as well, from the wrapped ``receive``,
    which is what keeps the bytes past the limit from ever being buffered.
    """


class TokenInvalid(ValueError):
    """Raised when a capability token cannot be redeemed.

    Deliberately one class with one message for every cause — unknown,
    expired, already spent, or presented at the wrong kind of route. Telling
    them apart is free intelligence for whoever is guessing: "expired" says a
    token once existed, and "wrong kind" says which route to try next.
    """


#: The single message :class:`TokenInvalid` ever carries.
INVALID_TOKEN_MESSAGE = "invalid or expired token"


def _connect(path: str | Path | None) -> sqlite3.Connection:
    """Open a connection and apply any pending migrations (idempotent)."""
    conn = db.connect(path)
    db.init_db(conn)
    return conn


def public_base_url() -> str:
    """Return the base URL minted URLs are built on, without a trailing slash.

    Returns:
        ``NODUM_PUBLIC_URL`` when set and non-empty, else
        :data:`DEFAULT_PUBLIC_URL`.
    """
    configured = os.environ.get(PUBLIC_URL_ENV, "").strip()
    return (configured or DEFAULT_PUBLIC_URL).rstrip("/")


def _grant_url(kind: str, secret: str, base_url: str | None) -> str:
    """Build the redeemable URL for one token."""
    base = (base_url.strip() if base_url else public_base_url()).rstrip("/")
    return f"{base}{TOKEN_PATHS[kind]}/{secret}"


def _checked_ttl(ttl_seconds: int) -> int:
    """Validate a caller's TTL against the bounds.

    Raises:
        ValueError: If the TTL is not between 1 and :data:`MAX_TTL_SECONDS`.
    """
    if not 1 <= ttl_seconds <= MAX_TTL_SECONDS:
        raise ValueError(f"ttl_seconds must be between 1 and {MAX_TTL_SECONDS}, got {ttl_seconds}")
    return ttl_seconds


def _sweep_expired(conn: sqlite3.Connection) -> None:
    """Drop dead token rows on the way in (the caller commits).

    Expiry is otherwise only noticed when a dead URL is presented, so a token
    nobody comes back for is never deleted at all — the same leak
    :func:`nodum.auth.create_session` sweeps for sessions. Nothing is lost:
    the mint and any redemption are in the event log, which is the audit
    record; the row is only the live authority.
    """
    conn.execute("DELETE FROM url_tokens WHERE expires_at <= datetime('now')")


def _expiry_in(conn: sqlite3.Connection, ttl_seconds: int) -> str:
    """Compute the expiry timestamp in SQLite, so it matches what is compared."""
    return conn.execute("SELECT datetime('now', ?)", (f"+{ttl_seconds} seconds",)).fetchone()[0]


def mint_download(
    id_or_hash: str,
    *,
    principal: Principal,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    base_url: str | None = None,
    path: str | Path | None = None,
) -> UrlGrantOut:
    """Mint a short-lived, single-use URL for an asset's original bytes.

    The asset is resolved through :func:`nodum.assets.get_asset`, so the
    principal's reach decides what exists: an asset described only in a space
    it holds nothing on answers *not found*, and no token is written. The
    secret is returned here and nowhere else — the row keeps its sha-256.

    Args:
        id_or_hash: Asset hash, or the id of a describing ``asset_ref`` node.
        principal: Who is asking; also the actor a later redemption is
            attributed to, since the URL itself carries no identity.
        ttl_seconds: Lifetime, 1 to :data:`MAX_TTL_SECONDS`
            (default :data:`DEFAULT_TTL_SECONDS`).
        base_url: Override the address the URL is built on
            (default :func:`public_base_url`).
        path: Explicit database path; defaults to ``NODUM_DB`` resolution.

    Returns:
        The grant: the one copy of the token, its ready-to-use URL, the
        asset's hash, and the expiry.

    Raises:
        AssetNotFound: If no asset the principal can reach resolves.
        ValueError: If ``ttl_seconds`` is outside its bounds.
    """
    ttl = _checked_ttl(ttl_seconds)
    asset = assets.get_asset(id_or_hash, principal=principal, path=path)
    token_id = uuid.uuid4().hex
    secret, secret_hash = auth.generate_token()

    conn = _connect(path)
    try:
        _sweep_expired(conn)
        expires_at = _expiry_in(conn, ttl)
        conn.execute(
            """
            INSERT INTO url_tokens (id, token_hash, kind, asset_hash, created_by, expires_at)
            VALUES (?, ?, 'download', ?, ?, ?)
            """,
            (token_id, secret_hash, asset.hash, principal.actor_string, expires_at),
        )
        conn.commit()
    finally:
        conn.close()

    # The event is a second transaction, and the order matters: the secret
    # exists only in this frame until it is returned, so a failure between the
    # row and its log entry leaves a capability nobody can ever present —
    # whereas logging first would record a grant that was never issued.
    service.record_asset_event(
        MINT_OPS["download"],
        _mint_payload(token_id, "download", asset.hash, expires_at, principal.actor_string),
        principal=principal,
        path=path,
    )
    return UrlGrantOut(
        kind="download",
        token=secret,
        url=_grant_url("download", secret, base_url),
        asset_hash=asset.hash,
        expires_at=expires_at,
    )


def mint_upload(
    name: str,
    mime: str,
    size: int,
    *,
    sha256: str | None = None,
    space: str | None = None,
    principal: Principal,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    base_url: str | None = None,
    path: str | Path | None = None,
) -> UploadGrantOut:
    """Mint a short-lived, single-use URL to PUT one file to — or skip it.

    A declared ``sha256`` the principal can already reach is answered with the
    existing asset and **no grant at all**: the bytes are already here, so no
    bytes move (design §5.7 rule 4). That shortcut is still event-logged — a
    dedup hit is exactly the kind of thing worth seeing in the log — and it is
    scoped like every other asset read, so an unreachable hash is treated as
    unknown and gets an ordinary grant rather than a yes/no oracle.

    Args:
        name: The original name the bytes will arrive under.
        mime: The declared content type.
        size: The declared size in bytes; the ceiling the upload route
            enforces on the body. Must not exceed :data:`MAX_UPLOAD_BYTES`.
        sha256: The caller's declared content hash, lowercase hex, enabling
            the dedup shortcut.
        space: Target space id or name for the describing node the ingestion
            will write (default: the ``main`` space). The principal must be
            able to write it, or the grant would be unusable.
        principal: Who is asking; also the actor a later redemption is
            attributed to.
        ttl_seconds: Lifetime, 1 to :data:`MAX_TTL_SECONDS`.
        base_url: Override the address the URL is built on.
        path: Explicit database path; defaults to ``NODUM_DB`` resolution.

    Returns:
        Either ``grant`` (the single-use upload URL) or ``asset`` (the dedup
        hit) — never both, never neither.

    Raises:
        PayloadTooLarge: If ``size`` is above :data:`MAX_UPLOAD_BYTES` — the
            grant would promise more than the server will read.
        ValueError: If ``size`` is negative, if ``sha256`` is not lowercase hex
            sha-256, or if ``ttl_seconds`` is outside its bounds.
        TypeNotFound: If ``space`` does not resolve for this principal.
        GrantNotPermitted: If the principal cannot write the target space.
    """
    ttl = _checked_ttl(ttl_seconds)
    if size > MAX_UPLOAD_BYTES:
        # The declared size is over the ceiling: that is a payload too large,
        # which already has a class and a 413, and a browser rendering a bare
        # `ValueError: size must be between …` for it was the whole bug.
        raise PayloadTooLarge(
            f"this server will not read more than {MAX_UPLOAD_BYTES} bytes in one "
            f"upload; the declared size is {size}"
        )
    if size < 0:
        raise ValueError(f"size must be a non-negative byte count, got {size}")
    if sha256 is not None and not SHA256_RE.match(sha256):
        raise ValueError("sha256 must be a lowercase hex sha-256 digest")

    if sha256 is not None:
        try:
            existing = assets.get_asset(sha256, principal=principal, path=path)
        except assets.AssetNotFound:
            existing = None
        if existing is not None:
            service.record_asset_event(
                MINT_OPS["upload"],
                _mint_payload(None, "upload", existing.hash, None, principal.actor_string)
                | {"dedup": True, "original_name": name, "mime": mime, "size_bytes": size},
                principal=principal,
                path=path,
            )
            return UploadGrantOut(grant=None, asset=existing)

    token_id = uuid.uuid4().hex
    secret, secret_hash = auth.generate_token()
    # The service's own space resolver, not a second copy of it: it is where
    # "an ungranted space and a nonexistent one answer identically" (Q13 review
    # S3) is implemented, and a duplicate would drift off it.
    space_id = service.resolve_space_id(space, principal=principal, path=path)
    conn = _connect(path)
    try:
        # Refuse now rather than after the transfer: a grant whose describing
        # node the principal could never write is a wasted upload.
        Store(conn, principal).landing_state(space_id)
        _sweep_expired(conn)
        expires_at = _expiry_in(conn, ttl)
        conn.execute(
            """
            INSERT INTO url_tokens (id, token_hash, kind, asset_hash, original_name, mime,
                                    max_bytes, space_id, created_by, expires_at)
            VALUES (?, ?, 'upload', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token_id,
                secret_hash,
                sha256,
                name,
                mime,
                size,
                space_id,
                principal.actor_string,
                expires_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    service.record_asset_event(
        MINT_OPS["upload"],
        _mint_payload(token_id, "upload", sha256, expires_at, principal.actor_string)
        | {
            "dedup": False,
            "original_name": name,
            "mime": mime,
            "max_bytes": size,
            "space_id": space_id,
        },
        principal=principal,
        path=path,
    )
    return UploadGrantOut(
        grant=UrlGrantOut(
            kind="upload",
            token=secret,
            url=_grant_url("upload", secret, base_url),
            asset_hash=sha256,
            expires_at=expires_at,
            max_bytes=size,
        ),
        asset=None,
    )


def consume(token: str, *, kind: str, path: str | Path | None = None) -> sqlite3.Row:
    """Redeem a capability token exactly once and return its row.

    The spend is a single ``UPDATE`` guarded on ``used_at IS NULL`` and on the
    expiry, and the caller wins iff it matched a row: there is no window
    between checking and marking for a second redemption to slip through. A
    token presented at the wrong kind of route is refused **without** being
    spent, so a stray request cannot burn someone else's grant.

    The redemption is recorded in the same transaction as the spend — a
    consumed token with no audit entry would be the one gap the design's
    "log both ends" rule exists to close. It is attributed to the token's
    ``created_by``: a capability URL carries no ambient credential, so the
    only truthful actor is the principal who authorised it.

    Args:
        token: The secret from the URL.
        kind: ``download`` or ``upload`` — the route this arrived at.
        path: Explicit database path; defaults to ``NODUM_DB`` resolution.

    Returns:
        The token's row (``id``, ``kind``, ``asset_hash``, ``original_name``,
        ``mime``, ``max_bytes``, ``space_id``, ``created_by``, ``expires_at``,
        ``used_at``, ``created_at``), for the route to act on.

    Raises:
        ValueError: If ``kind`` is not a known token kind.
        TokenInvalid: If the token is unknown, expired, already spent, or of
            another kind — one class, one message, for all four.
    """
    if kind not in TOKEN_PATHS:
        raise ValueError(f"kind must be one of {tuple(TOKEN_PATHS)}, got {kind!r}")
    # `auth`'s own hash, not a second scheme: `generate_token` minted this
    # secret, so the function that turns it into a stored key has to be the
    # same one, or the two drift and nothing ever verifies again.
    token_hash = auth._token_hash(token)
    conn = _connect(path)
    try:
        cursor = conn.execute(
            """
            UPDATE url_tokens SET used_at = datetime('now')
            WHERE token_hash = ? AND kind = ? AND used_at IS NULL
              AND expires_at > datetime('now')
            """,
            (token_hash, kind),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            raise TokenInvalid(INVALID_TOKEN_MESSAGE)
        row = conn.execute(
            "SELECT * FROM url_tokens WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        # Written on this connection, inside the spend's transaction: a token
        # marked used with no audit entry would be exactly the gap the "log
        # both ends" rule exists to close. The actor is the token row's own
        # `created_by` — a capability URL carries no ambient credential, so
        # there is no live principal to attribute this to, and the identity
        # comes from stored state rather than from whoever presented the URL.
        service.record_asset_event(
            REDEEM_OPS[kind],
            _redeem_payload(row),
            actor=row["created_by"],
            conn=conn,
        )
        conn.commit()
        return row
    finally:
        conn.close()


def _mint_payload(
    token_id: str | None,
    kind: str,
    asset_hash: str | None,
    expires_at: str | None,
    actor: str,
) -> dict[str, Any]:
    """The audit payload for a mint: the intent, never the secret."""
    return {
        "token_id": token_id,
        "kind": kind,
        "asset_hash": asset_hash,
        "expires_at": expires_at,
        "actor": actor,
    }


def _redeem_payload(row: sqlite3.Row) -> dict[str, Any]:
    """The audit payload for a redemption, keyed explicitly.

    Built field by field rather than from ``dict(row)`` on purpose: the row
    carries ``token_hash``, and a payload is JSON in a log everything reads.
    """
    return {
        "token_id": row["id"],
        "kind": row["kind"],
        "asset_hash": row["asset_hash"],
        "original_name": row["original_name"],
        "mime": row["mime"],
        "max_bytes": row["max_bytes"],
        "space_id": row["space_id"],
        "expires_at": row["expires_at"],
        "actor": row["created_by"],
    }
