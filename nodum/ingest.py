"""The ingestion pipeline (design §5.5–§5.7) — bytes in, reviewable subgraph out.

This is the deterministic half of Phase 4. A dropped file or a URL becomes:

* an **asset** — content-addressed bytes in the blob store (idempotent dedup);
* an **``asset_ref`` node** describing those bytes *in one space*, which is what
  makes the asset reachable at all (:mod:`nodum.assets`, note 01 D1);
* a **``source`` node** carrying the extracted text as its content;
* a ``derived_from`` edge from the source to the bytes it came out of;
* one ``block`` child per page of text, for paginated formats.

**Claim extraction is deliberately not here** (note 01 D3). Deciding that a
sentence is a claim is a judgement call and belongs to the research agent in
design §3 — Phase 5. Splitting prose into sentences and calling each one a
claim would fill the review queue with noise rather than knowledge, so Phase 4
proposes *sources and structure* and stops there.

**Every graph write goes through the public :mod:`nodum.service` API.** The
landing state, the grant checks, the event log, and wikilink materialisation
are the service's business as always: an agent with ``suggest`` gets a whole
subgraph in ``proposed``, one with ``edit`` gets it live. Ingestion adds no
authority of its own — it is a composition, not a second writer.

**Extracted text lives in two places on purpose.** The full text goes on
``assets.extracted_text``, where the FTS projector joins it onto the
``asset_ref`` node's index row and BM25 can reach every word of a long
document. A capped copy becomes the ``source`` node's *content*, which is what
the vector projector chunks and embeds — semantic search only ever sees node
text. One store would have cost one of the two signals.

**Ingestion is idempotent, and that is what makes a partial failure
recoverable.** Registration is content-addressed, and migration 0009's unique
index allows one live ``asset_ref`` per ``(hash, space)`` — so a re-run finds
the describing node instead of tripping the index. A run interrupted between
the two node writes is repaired by running it again: the existing
``asset_ref`` is reused and only the missing half is created. The repair
covers the whole subgraph, not just the nodes: a re-run that finds both nodes
but not the ``derived_from`` edge or the page blocks recreates the missing
half too, so **re-running an interrupted ingestion converges on the complete
subgraph** (finding M27).

Network posture for :func:`ingest_url`: ``http``/``https`` only, one bounded
read with a per-read timeout **and a wall-clock deadline** — the socket
timeout bounds one read, so a server dripping a chunk inside every window
would otherwise hold the fetch open forever, and
:data:`FETCH_DEADLINE_SECONDS` is what cuts that class off (finding M26).
Redirects are confined to the same two schemes (urllib would otherwise follow
one to ``ftp:``), and a **type policy** refuses a fetched body whose stored
MIME no extraction handler claims, before any byte reaches the blob store.
It does **not** block loopback or private-range addresses — the server is
itself a loopback service and its own test fixture is one, so a blocklist
would be theatre that broke the suite. Anything that can call ``ingest_url``
can already reach the machine's network position; that is a property of
granting an agent ingestion, and it is stated here rather than half-defended.
"""

from __future__ import annotations

import mimetypes
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from email.message import Message
from pathlib import Path
from typing import Any

from nodum import assets, auth, extract, service
from nodum.models import AssetOut, ExtractionOut, IngestOut, NodeOut
from nodum.principal import Principal

#: Node type describing an asset's bytes inside one space (design §5.2). One
#: live row per ``(hash, space)`` — migration 0009's unique index.
ASSET_REF_TYPE = "asset_ref"

#: Node type for the ingested document itself.
SOURCE_TYPE = "source"

#: Node type of the per-page children of a paginated source.
PAGE_TYPE = "block"

#: State a retired node carries, and so the one a page block is no longer
#: counted in (:func:`_existing_page_blocks`).
ARCHIVED_STATE = "archived"

#: Edge tying a source to the bytes it was extracted from. Directed
#: source → asset_ref: the document is *derived from* the binary, and the
#: seed set carries ``derived`` as the inverse.
PROVENANCE_EDGE = "derived_from"

#: How much extracted text becomes the ``source`` node's content.
#:
#: The node content is the *embedding* input (the vec projector chunks it), and
#: an entire book in one node buys nothing a few hundred chunks do not already
#: give. The full text is never lost — it stays on ``assets.extracted_text``
#: for BM25 — so this cap trades nothing away, and a capped body says so in its
#: own last line rather than ending mid-sentence.
SOURCE_CONTENT_CHARS = 200_000

#: Marker appended to a source body that hit :data:`SOURCE_CONTENT_CHARS`.
TRUNCATION_MARKER = "\n\n… [truncated — the full extracted text is on the asset]"

#: Most per-page ``block`` children one ingestion will create.
#:
#: Page blocks are what make a PDF *reviewable* rather than a single opaque
#: wall of text, but under a ``suggest`` grant every one of them is a row in a
#: human's review queue — so a 900-page scan must not become a 900-item queue.
#: Pages past the cap are reported through ``pages_truncated``; nothing is ever
#: dropped silently.
MAX_PAGE_BLOCKS = 100

#: URL schemes :func:`ingest_url` will fetch, on the way in and across every
#: redirect.
FETCHABLE_SCHEMES = frozenset({"http", "https"})

#: Ceiling on a fetched body, enforced while streaming rather than after.
MAX_FETCH_BYTES = 64 * 1024 * 1024

#: Socket timeout for one read of a fetch, in seconds.
FETCH_TIMEOUT_SECONDS = 30

#: Wall-clock cap on a whole fetch, independent of the per-read socket timeout.
#:
#: ``FETCH_TIMEOUT_SECONDS`` bounds one read, not the fetch: a server dripping
#: one chunk inside every 30 s window keeps the read loop alive indefinitely,
#: well past anything a reader could consume. 60 is a full socket timeout for
#: the connect and headers, plus a body that must then deliver in good time —
#: a legitimate document finishes in seconds, so the deadline only cuts the
#: slow-drip class, and it cuts it at a bounded wall-clock rather than at the
#: byte ceiling's mercy (finding M26).
FETCH_DEADLINE_SECONDS = 60

#: Name given to a fetched document whose URL and headers offer none.
FALLBACK_FETCH_NAME = "download"


class IngestError(ValueError):
    """Raised when a source cannot be ingested (bad URL, oversized fetch, not a file)."""


def ingest_file(
    source: str | Path,
    *,
    name: str | None = None,
    space: str | None = None,
    title: str | None = None,
    principal: Principal,
    path: str | Path | None = None,
) -> IngestOut:
    """Ingest one local file: register the bytes, extract, describe, propose.

    Args:
        source: Path to the local file. The server reads it directly — this is
            design §5.7's "ingestion by reference", which is why no base64 ever
            crosses MCP.
        name: Original name to record (defaults to the file's own). It is also
            the MIME hint, so it decides which extraction handler runs.
        space: Target space id or name (default: the ``main`` space). The
            describing node lands here, and *that* is what makes the asset
            readable at all.
        title: Title for the ``source`` node (defaults to ``name``).
        principal: Who is ingesting. Their grant on ``space`` decides whether
            the resulting subgraph is ``active`` or ``proposed``.
        path: Explicit database path.

    Returns:
        The asset, the nodes and edges that describe it, and what extraction
        got out of it. ``created`` is false when the asset already had a
        describing node in this space.

    Raises:
        IngestError: If ``source`` is not a regular file.
        GrantNotPermitted: If the principal may not write the target space.
    """
    source_file = Path(source).expanduser()
    if not source_file.is_file():
        raise IngestError(f"not a file: {source_file}")
    return _ingest(
        source_file,
        name=name or source_file.name,
        origin=str(source_file),
        origin_kind="file",
        space=space,
        title=title,
        principal=principal,
        path=path,
    )


def ingest_url(
    url: str,
    *,
    name: str | None = None,
    space: str | None = None,
    title: str | None = None,
    principal: Principal,
    path: str | Path | None = None,
) -> IngestOut:
    """Fetch a URL into the blob store and ingest it exactly like a local file.

    The fetched bytes are spooled to a temporary file and registered from
    there, so the streaming registration path — and its re-hash check — is the
    same one the CLI uses. The URL is recorded on both nodes' props as
    provenance.

    Args:
        url: An ``http`` or ``https`` URL.
        name: Original name to record. Defaults to the URL's own filename, or
            one derived from the response's ``Content-Type`` when the URL has
            no usable name — the extension is what picks the handler, so a
            document served from an extensionless path still extracts.
        space: Target space id or name (default: the ``main`` space).
        title: Title for the ``source`` node.
        principal: Who is ingesting.
        path: Explicit database path.

    Returns:
        The same result shape as :func:`ingest_file`.

    Raises:
        IngestError: If the scheme is not fetchable, the fetch fails, the body
            passes :data:`MAX_FETCH_BYTES`, the fetch does not finish within
            :data:`FETCH_DEADLINE_SECONDS`, or the fetched body's stored type
            no extraction handler claims (finding M26).
        GrantNotPermitted: If the principal may not write the target space.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in FETCHABLE_SCHEMES:
        raise IngestError(
            f"cannot fetch {parsed.scheme or 'schemeless'} URLs: "
            f"ingest_url takes {', '.join(sorted(FETCHABLE_SCHEMES))}"
        )
    with tempfile.TemporaryDirectory(prefix="nodum-ingest-") as workspace:
        spooled = Path(workspace) / "body"
        content_type, header_name = _fetch(url, spooled)
        resolved = name or _fetched_name(url, header_name, content_type)
        _refuse_unsupported_fetch(spooled, resolved, url)
        return _ingest(
            spooled,
            name=resolved,
            origin=url,
            origin_kind="url",
            space=space,
            title=title,
            principal=principal,
            path=path,
        )


def ingest_upload(
    token_row: Mapping[str, Any] | sqlite3.Row,
    source_file: str | Path,
    *,
    path: str | Path | None = None,
) -> IngestOut:
    """Ingest bytes delivered through a redeemed upload capability.

    Design §5.7 rule 4 ends "normal ingestion runs after the PUT", and this is
    that step. Without it the upload hatch dead-ends: an agent host with no
    shared filesystem could deliver bytes that no surface could then turn into
    a subgraph, since :func:`ingest_file` takes a path on the *server*.

    It lives here rather than in the HTTP adapter because it needs a principal,
    and that adapter is structurally forbidden from minting one — identity
    there comes from a verified session or not at all. The capability has no
    session by design, so the principal is re-minted from the token row's
    ``created_by``: the account that authorised the upload, recorded when it
    was still authenticated. A disabled account fails here, so a capability
    cannot outlive the revocation of the principal behind it.

    Args:
        token_row: The ``url_tokens`` row :func:`nodum.urls.consume` returned.
        source_file: The delivered bytes, already spooled to disk.
        path: Explicit database path.

    Returns:
        The same result shape as :func:`ingest_file`.

    Raises:
        UnknownPrincipal: If the row names no account.
        PrincipalDisabled: If that account has since been disabled.
        GrantNotPermitted: If the account's write grant on the target space has
            been revoked since the grant was minted.
    """
    principal = auth.principal_from_actor(token_row["created_by"], path=path)
    return ingest_file(
        source_file,
        name=token_row["original_name"],
        space=token_row["space_id"],
        principal=principal,
        path=path,
    )


#: The MIME types :func:`ingest_url` admits, as the handler registry's own
#: claims (extract.py) restated for a refusal message. Derived rather than
#: copied so a handler added or removed there changes the policy and its
#: message together — ``handler_for`` decides admission, this only names it.
FETCHABLE_MIME_CLAIMS: tuple[str, ...] = tuple(
    sorted({mime for handler in extract.REGISTRY for mime in handler.mimes})
)


def _refuse_unsupported_fetch(source: Path, name: str, url: str) -> None:
    """Refuse a fetched body whose stored type no extraction handler claims.

    The type is decided the same way registration will record it — the sniff
    weighed against the name (:func:`assets.stored_mime`) — because extraction
    dispatches on the *stored* MIME: a misnamed URL is exactly the case where
    the name and the bytes disagree, and the stored type is the one that
    decides the pipeline's behaviour. The refusal runs **before** ``_ingest``
    (and therefore before ``register_asset``), so no byte reaches the blob
    store for a body the pipeline cannot act on — registration is the
    irreversible half, and an unsupported type is not an absent dependency:
    the URL is fetchable but not ingestible, and reporting an empty
    extraction would claim success for a failed ingest. The file path
    deliberately has no such policy (finding M26).

    Args:
        source: The spooled fetched body.
        name: The name registration would record; its extension is the guess
            ``_stored_mime`` weighs against the sniff.
        url: The fetched URL, named in the refusal as the caller's input.

    Raises:
        IngestError: If :func:`extract.handler_for` claims no handler for the
            type registration would store.
    """
    stored = assets.stored_mime(source, name=name)
    if extract.handler_for(stored) is not None:
        return
    raise IngestError(
        f"{url} is {stored}, which no extraction handler claims; "
        f"ingest_url admits {', '.join(FETCHABLE_MIME_CLAIMS)}"
    )


def _ingest(
    source_file: Path,
    *,
    name: str,
    origin: str,
    origin_kind: str,
    space: str | None,
    title: str | None,
    principal: Principal,
    path: str | Path | None,
) -> IngestOut:
    """Run the pipeline over a file already on disk (the shared half of both entry points)."""
    # Resolve the target space and probe the write grant **before** any bytes
    # are stored. Registration is the irreversible half of this function — there
    # is no delete route — so the two refusals that need no bytes, an
    # unresolvable target space and a missing write grant, must happen first:
    # an upload grant minted against a space archived inside its five-minute TTL
    # used to store up to 32 MiB with no describing node, no FTS row, and no way
    # to reclaim them (review F13), and a read-only agent's refused ingest
    # committed the same bytes with the same permanence, because the write grant
    # was only demanded by the node write afterwards (review B6). The grant
    # probe is the same authority the node write itself uses
    # (`service.require_write_grant` → `Store.landing_state`), so this refusal
    # is exactly the one the write would have given, moved before any byte is
    # stored.
    target_space = service.resolve_space_id(space, principal=principal, path=path)
    service.require_write_grant(target_space, principal=principal, path=path)
    # The describing nodes are typed asset_ref/source, which live in meta — a
    # principal that cannot read meta can never write them, and that refusal
    # must not wait until after the bytes are stored either.
    service.require_type_read(ASSET_REF_TYPE, principal=principal, path=path)
    service.require_type_read(SOURCE_TYPE, principal=principal, path=path)
    asset = assets.register_asset(source_file, name=name, path=path)

    existing_ref = service.find_by_asset_hash(
        asset.hash, type=ASSET_REF_TYPE, space_id=target_space, principal=principal, path=path
    )
    existing_source = service.find_by_asset_hash(
        asset.hash, type=SOURCE_TYPE, space_id=target_space, principal=principal, path=path
    )
    if existing_ref is not None and existing_source is not None:
        return _already_ingested(
            asset,
            existing_ref,
            existing_source,
            source_file=source_file,
            principal=principal,
            path=path,
        )

    extraction = extract.extract(source_file, mime=asset.mime)
    if extraction.text:
        assets.set_extracted_text(asset.hash, extraction.text, path=path)
    # The page numbers this run will write, recorded before the writes so the
    # describing node carries them: a later re-run can detect (cheaply) that
    # the page half of an interrupted ingestion is missing, instead of
    # re-running the handler to learn the same thing — the OCR/transcription
    # cost the already-ingested branch exists to avoid (finding M27).
    page_numbers = _page_block_numbers(extraction.pages)

    provenance: dict[str, Any] = {"asset_hash": asset.hash, "mime": asset.mime}
    if origin_kind == "url":
        provenance["url"] = origin

    asset_ref = existing_ref or service.create_node(
        type=ASSET_REF_TYPE,
        title=name,
        props={
            **provenance,
            "size_bytes": asset.size_bytes,
            "original_name": asset.original_name,
            "extracted_by": extraction.handler,
            "extracted_chars": len(extraction.text),
            "extracted_pages": page_numbers,
        },
        space=space,
        principal=principal,
        path=path,
    )
    source = service.create_node(
        type=SOURCE_TYPE,
        title=title or name,
        content=_source_content(extraction.text),
        props={**provenance, "original_name": asset.original_name},
        space=space,
        principal=principal,
        path=path,
    )
    edges = [
        service.create_edge(
            source.id, asset_ref.id, PROVENANCE_EDGE, principal=principal, path=path
        )
    ]
    pages, pages_truncated = _create_page_blocks(
        extraction.pages, parent=source, space=space, principal=principal, path=path
    )

    event_seq = service.record_asset_event(
        "asset.ingest",
        {
            "asset_hash": asset.hash,
            "mime": asset.mime,
            "size_bytes": asset.size_bytes,
            "original_name": asset.original_name,
            "origin": origin,
            "origin_kind": origin_kind,
            "space_id": asset_ref.space_id,
            "handler": extraction.handler,
            "extracted_chars": len(extraction.text),
            "extraction_detail": extraction.detail,
            "asset_ref_id": asset_ref.id,
            "source_id": source.id,
            "page_ids": [page.id for page in pages],
            "pages_truncated": pages_truncated,
            "edge_ids": [edge.id for edge in edges],
            "created": existing_ref is None,
        },
        principal=principal,
        path=path,
    )
    return IngestOut(
        # Reported from what this call already knows, **not** re-read through
        # the scoped accessor. Under a `suggest` grant the describing node
        # lands `proposed`, and a proposed description does not make an asset
        # readable (note 01 D1) — so an agent would be refused the metadata of
        # the very bytes it just ingested. Echoing back the result of one's own
        # write is not a scoped read of someone else's asset.
        asset=asset.model_copy(update={"extracted_text": extraction.text or None}),
        asset_ref=asset_ref,
        source=source,
        pages=pages,
        pages_truncated=pages_truncated,
        edges=edges,
        extraction=ExtractionOut(
            handler=extraction.handler,
            chars=len(extraction.text),
            pages=len(extraction.pages),
            detail=extraction.detail,
        ),
        created=existing_ref is None,
        event_seq=event_seq,
    )


def _already_ingested(
    asset: AssetOut,
    asset_ref: NodeOut,
    source: NodeOut,
    *,
    source_file: Path,
    principal: Principal,
    path: str | Path | None,
) -> IngestOut:
    """Build the result for an asset this space already describes — after
    repairing the half an interrupted run may have left unwritten.

    The gate that reaches this branch checks the two nodes, not the whole
    subgraph, and finding M27 is the difference that makes: a run interrupted
    after both node writes but before the ``derived_from`` edge or the page
    blocks leaves those halves missing, and this branch used to report
    "already ingested" over the partial subgraph forever. It now reads the
    graph for the two remaining halves and recreates whichever is missing —
    the edge first, then each page block whose recorded number has no block
    in any state (an archived block was *written*, and a human's archive is
    curation, not an interruption to repair). The recorded ``extracted_pages``
    prop is what makes the page half detectable without re-running the
    handler; an asset registered before the prop existed cannot be page-
    repaired, and its edge half still is. ``created`` reports what this call
    actually wrote: true when it recreated the missing half, false when the
    subgraph was complete and nothing was written — so a caller is never told
    "complete" over a subgraph the repair just finished, nor "created" over
    one it left untouched.

    Extraction is skipped rather than repeated on the complete subgraph: the
    text is already on the asset and in the source node, and re-running a
    handler would cost an OCR pass or a transcription to arrive at the same
    bytes. The repair re-runs it only when a recorded page number is missing,
    because the page *text* is the one thing the graph cannot reconstruct
    (the handler that did run is read back from the describing node's props,
    so the report stays truthful instead of claiming a fresh extraction).
    The repair writes no ``asset.ingest`` event of its own — the node and
    edge creates it performs are logged individually, and this is a repair,
    not a fresh ingest.
    """
    extracted = asset.extracted_text or ""
    pages, blocks_ever_written, page_numbers = _existing_page_blocks(
        source, principal=principal, path=path
    )
    edges = service.list_edges(
        node_id=source.id, type=PROVENANCE_EDGE, principal=principal, path=path
    )
    provenance = next(
        (edge for edge in edges if edge.src_id == source.id and edge.dst_id == asset_ref.id),
        None,
    )
    expected = [
        number
        for number in (asset_ref.props.get("extracted_pages") or [])
        if isinstance(number, int)
    ]
    missing = [number for number in expected if number not in page_numbers]

    detail = "already ingested into this space; nothing re-extracted"
    pages_extracted = 0
    created = False
    if provenance is None or missing:
        if provenance is None:
            service.create_edge(
                source.id, asset_ref.id, PROVENANCE_EDGE, principal=principal, path=path
            )
            created = True
        if missing:
            # The page text is the one thing the graph cannot reconstruct, so
            # the repair re-runs the handler — the same cost the two-node
            # repair already pays on its path — and writes only the blocks
            # whose numbers are recorded but absent.
            extraction = extract.extract(source_file, mime=asset.mime)
            pages_extracted = len(extraction.pages)
            for number in missing:
                text = extraction.pages[number - 1] if number - 1 < len(extraction.pages) else ""
                if not text.strip():
                    continue
                service.create_node(
                    type=PAGE_TYPE,
                    title=f"Page {number}",
                    content=text,
                    parent_id=source.id,
                    props={"page": number, "asset_hash": source.props.get("asset_hash")},
                    space=source.space_id,
                    principal=principal,
                    path=path,
                )
                created = True
        repaired_parts = []
        if provenance is None:
            repaired_parts.append("the provenance edge")
        if missing:
            repaired_parts.append("the page blocks")
        detail = (
            "already ingested into this space; recreated "
            f"{' and '.join(repaired_parts)} an interrupted run left behind"
        )
        pages, blocks_ever_written, page_numbers = _existing_page_blocks(
            source, principal=principal, path=path
        )
        edges = service.list_edges(
            node_id=source.id, type=PROVENANCE_EDGE, principal=principal, path=path
        )
    return IngestOut(
        asset=asset,
        asset_ref=asset_ref,
        source=source,
        pages=pages,
        # Inferred rather than recorded: this branch re-extracts nothing, so the
        # block count is the only evidence left that the cap bit. A document
        # whose text ran to exactly `MAX_PAGE_BLOCKS` pages is then reported as
        # truncated when it was not — and that is the right side to err on, since
        # the other one has a 900-page scan answering `false` on the second drop,
        # which is the silent truncation this pipeline promises never to make.
        # Counted over every page block ever written, archived included: retiring
        # one page of a truncated scan does not un-truncate it.
        pages_truncated=blocks_ever_written >= MAX_PAGE_BLOCKS,
        edges=edges,
        extraction=ExtractionOut(
            handler=str(asset_ref.props.get("extracted_by") or "none"),
            chars=len(extracted),
            pages=pages_extracted,
            detail=detail,
        ),
        created=created,
        event_seq=0,
    )


def _existing_page_blocks(
    source: NodeOut, *, principal: Principal, path: str | Path | None
) -> tuple[list[NodeOut], int, set[int]]:
    """The page blocks a ``source`` node carries, and how many were ever written.

    ``service.list_children`` filters on neither type nor state, and a ``source``
    is an ordinary node: anything can be written as its child (the CLI's
    ``--parent-id``, MCP, any ``parent_id`` write), and an archived page block
    keeps its parent. Reporting every child made ``pages`` claim a hand-written
    note was a page of the document and kept counting a retired block — on the
    CLI, over MCP and in the browser at once, since all three read this one
    field (review F2). Restricting it here rather than at a caller is what keeps
    the two branches of one function answering the same question.

    **Two counts, because they answer different questions.** The list is what the
    document *has*, which is the created branch's meaning. The count is what the
    cap *did*, and it must include archived blocks: inferring the truncation flag
    from the filtered list let a single archived page turn a `true` into a
    `false` — reintroducing, through the very filter that fixed its sibling, the
    silent truncation the flag exists to prevent. The page-number set is over
    every block in any state for the same reason: an archived block was
    *written*, so it is evidence the half exists and must not be recreated by
    the interrupted-run repair (finding M27).

    :returns: The live page blocks, the number of page blocks in any state, and
        the page numbers carried by every block in any state.
    """
    page_blocks = [
        child
        for child in service.list_children(source.id, principal=principal, path=path)
        if child.type == PAGE_TYPE
    ]
    page_numbers = {child.props["page"] for child in page_blocks if "page" in child.props}
    return (
        [child for child in page_blocks if child.state != ARCHIVED_STATE],
        len(page_blocks),
        page_numbers,
    )


def _source_content(text: str) -> str:
    """Return the extracted text as a source node's body, capped and marked when cut."""
    if len(text) <= SOURCE_CONTENT_CHARS:
        return text
    return text[:SOURCE_CONTENT_CHARS] + TRUNCATION_MARKER


def _page_block_numbers(pages: list[str]) -> list[int]:
    """The 1-based page numbers the block loop would write for ``pages``.

    The block loop's own rule, restated once so the recorded numbers and the
    written blocks cannot drift: blank pages are skipped (the numbering stays
    honestly sparse) and :data:`MAX_PAGE_BLOCKS` caps what gets written, the
    overflow reported as truncation rather than dropped. Recorded on the
    ``asset_ref`` as ``extracted_pages``, which is what lets the
    already-ingested branch detect a missing page half without re-running the
    handler (finding M27).
    """
    numbers: list[int] = []
    for number, text in enumerate(pages, start=1):
        if not text.strip():
            continue
        if len(numbers) >= MAX_PAGE_BLOCKS:
            break
        numbers.append(number)
    return numbers


def _create_page_blocks(
    pages: list[str],
    *,
    parent: NodeOut,
    space: str | None,
    principal: Principal,
    path: str | Path | None,
) -> tuple[list[NodeOut], bool]:
    """Create one ``block`` child per page that carries text; return them and the cap flag.

    Blank pages are skipped — a scanned PDF with no OCR handler available would
    otherwise propose a hundred empty nodes — while the page number stays in
    props, so the numbering is honestly sparse rather than quietly renumbered.
    """
    numbers = _page_block_numbers(pages)
    created: list[NodeOut] = []
    for number in numbers:
        created.append(
            service.create_node(
                type=PAGE_TYPE,
                title=f"Page {number}",
                content=pages[number - 1],
                parent_id=parent.id,
                props={"page": number, "asset_hash": parent.props.get("asset_hash")},
                space=space,
                principal=principal,
                path=path,
            )
        )
    # The cap bit is the loop's own rule too: more non-blank pages existed than
    # the cap let through.
    non_blank = sum(1 for page in pages if page.strip())
    return created, non_blank > MAX_PAGE_BLOCKS


def _fetch(url: str, destination: Path) -> tuple[str | None, str | None]:
    """Stream a URL into ``destination``; return its ``Content-Type`` and filename hint.

    The module-level seam every test replaces — the suite never touches the
    network (note 02), and the one live-ish test drives a loopback
    ``http.server``.

    Raises:
        IngestError: If the request fails, redirects off ``http``/``https``,
            the body passes :data:`MAX_FETCH_BYTES`, or the fetch does not
            finish within :data:`FETCH_DEADLINE_SECONDS` (finding M26).
    """
    request = urllib.request.Request(url, headers={"User-Agent": "nodum-ingest"})
    opener = urllib.request.build_opener(_SchemeBoundRedirectHandler)
    started = time.monotonic()
    try:
        with opener.open(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            content_type = response.headers.get("Content-Type")
            filename = _content_disposition_name(response.headers.get("Content-Disposition"))
            written = 0
            with destination.open("wb") as handle:
                while chunk := response.read(1 << 20):
                    # The socket timeout bounds one read, so it never bounds
                    # the fetch: a server dripping one chunk inside every 30 s
                    # window keeps this loop alive indefinitely. The deadline
                    # is the wall-clock cap that cuts that class off — checked
                    # after the read, because an already-delivered chunk is
                    # not the problem and the overrun from one blocking read
                    # is bounded by the socket timeout itself.
                    if time.monotonic() - started > FETCH_DEADLINE_SECONDS:
                        raise IngestError(
                            f"{url} did not finish within the {FETCH_DEADLINE_SECONDS}-second "
                            "fetch deadline — download it and ingest the file instead"
                        )
                    written += len(chunk)
                    if written > MAX_FETCH_BYTES:
                        raise IngestError(
                            f"{url} is larger than the {MAX_FETCH_BYTES}-byte fetch ceiling — "
                            "download it and ingest the file instead"
                        )
                    handle.write(chunk)
    except urllib.error.URLError as exc:
        raise IngestError(f"could not fetch {url}: {exc.reason}") from exc
    return content_type, filename


class _SchemeBoundRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect that leaves ``http``/``https``.

    urllib's own handler also allows ``ftp:``, which turns one redirect into a
    different protocol against a different port — not a jump this pipeline has
    any reason to make.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102 - urllib hook
        if urllib.parse.urlparse(newurl).scheme not in FETCHABLE_SCHEMES:
            raise IngestError(f"refusing a redirect to {newurl!r}: only http/https are followed")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _content_disposition_name(header: str | None) -> str | None:
    """Pull a filename out of a ``Content-Disposition`` header, if it carries one.

    Parsed with :mod:`email.message` rather than by hand: the header's quoting
    and parameter-continuation rules are exactly what that parser is for.
    """
    if not header:
        return None
    message = Message()
    message["Content-Disposition"] = header
    filename = message.get_filename()
    return Path(filename).name if filename else None


def _fetched_name(url: str, header_name: str | None, content_type: str | None) -> str:
    """Choose the recorded name for a fetched body — and therefore its MIME.

    ``register_asset`` derives the MIME from the *name*, so a document served
    from an extensionless path would be stored as ``application/octet-stream``
    and reach no handler at all. The response's own ``Content-Type`` is the
    better answer, and appending its extension to the name is how that answer
    reaches the store through the existing content-addressed API.
    """
    if header_name:
        return header_name
    candidate = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).name
    if candidate and Path(candidate).suffix:
        return candidate
    stem = candidate or FALLBACK_FETCH_NAME
    declared = (content_type or "").split(";")[0].strip()
    suffix = mimetypes.guess_extension(declared) if declared else None
    return f"{stem}{suffix}" if suffix else stem
