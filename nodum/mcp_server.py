"""The MCP server adapter (design §8) — a thin FastMCP front over the service layer.

This surface is for **external agents**, and external agents may only *grow*
the graph. It exposes the design §8.1 v1 tool contract's **read tier**
(``get_node``, ``get_children``, ``search``, ``traverse``, ``list_types``,
``get_schema``, ``find_path``, ``history``, ``diff``, ``get_asset``) and
**additive tier** (``create_node``, ``update_node``, ``link``,
``propose_edges``) — nothing else.

``get_asset`` enforces the §5.7 binary policy structurally: agents receive
metadata plus a small derived rendition (``preview``/``thumb`` WebP image
block); original binaries are never served over MCP.

**Neither the review tier nor the curative tier is ever registered.** The
review tools (``accept``, ``reject``) belong to the §8.1 "write
(human)" tier — accepting is a *destructive* effect (it makes proposed
structure live and archives what that structure replaces), so it stays with
the human, on the CLI and the review API. The curative tools
(``merge_nodes``, ``retype``, ``supersede_edge``, ``bulk_relink``,
``consolidate``) are §8.2. Both groups are enforced the same way: they simply
do not exist here, so there is no runtime check to argue around — and
:mod:`nodum.service` refuses a non-human reviewer regardless of surface.

Identity: the agent's bearer token, from the ``NODUM_AGENT_TOKEN``
environment variable (the shape MCP client configs carry in their ``env``
blocks — a command-line token would leak into ``ps``). Verification mints
the agent's principal, and its grant set confines every tool call. Transport
is stdio — what MCP clients actually launch.

Every tool delegates to :mod:`nodum.service` / :mod:`nodum.search`; there is
no logic here beyond argument mapping and JSON shaping.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from nodum import assets, auth, service
from nodum import search as search_module

#: Tool annotations per registered tier (design §8). Reads are read-only.
#: Additive writes only ever *add* state — a node, an edge, a proposed
#: version — whatever grant the agent holds, so ``destructiveHint=False``
#: stays true under ``edit`` as well as ``suggest``.
#:
#: ``update_node`` is the exception (Q13 review S15): under an ``edit`` grant
#: it overwrites the node's fields in place and can retire the mentions its
#: old content carried. MCP hosts auto-approve on ``destructiveHint=False``,
#: so annotating it that way was a lie told to the approval prompt — it is
#: marked destructive, and the cost is that an ``edit``-granted agent's
#: updates get a confirmation an additive tool's do not. Nothing here is
#: annotated by what the *current* agent may do: annotations are static
#: registry metadata, so each one states the worst case its grant allows.
_READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
_ADDITIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
_OVERWRITING = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

#: Curative tools (design §8.2) — asserted absent from the registry in tests.
CURATIVE_TOOLS = ("merge_nodes", "retype", "supersede_edge", "bulk_relink", "consolidate")

#: Review tools (design §8.1 "write (human)" tier). Like the curative
#: tools these are **never registered** — asserted absent in tests. Accepting
#: archives the active structure a proposal replaces, so it is destructive and
#: human-only; the CLI (``nodum review …``) is where it lives.
REVIEW_TOOLS = ("accept", "reject")

#: The tools this server registers, by tier (documentation + test anchor).
READ_TOOLS = (
    "get_node",
    "get_children",
    "search",
    "traverse",
    "list_types",
    "get_schema",
    "find_path",
    "history",
    "diff",
    "get_asset",
)
ADDITIVE_TOOLS = ("create_node", "update_node", "link", "propose_edges")

#: The write tools whose worst case (under an ``edit`` grant) overwrites live
#: state rather than adding to it — annotated ``destructiveHint=True``.
OVERWRITING_TOOLS = ("update_node",)


def _dump(result: BaseModel | list[BaseModel]) -> dict[str, Any] | list[dict[str, Any]]:
    """Serialise service results exactly like every other adapter."""
    if isinstance(result, list):
        return [item.model_dump(mode="json") for item in result]
    return result.model_dump(mode="json")


def create_server(*, token: str, db_path: str | Path | None = None) -> FastMCP:
    """Build the nodum MCP server bound to one verified agent principal.

    Args:
        token: The agent's bearer token (``ndm_…``, minted by
            ``nodum agent create`` / ``token-rotate``). Verified against its
            stored hash; the agent's grants then confine every tool call.
        db_path: Explicit database path; defaults to ``NODUM_DB`` resolution.

    Returns:
        A FastMCP server with the read and additive tools registered. Review
        tools (§8.1 human tier) and curative tools (§8.2) are never
        registered.

    Raises:
        auth.InvalidCredentials: If the token verifies no enabled agent.
    """
    principal = auth.verify_agent_token(token, path=db_path)
    server = FastMCP(
        "nodum",
        instructions=(
            "nodum knowledge graph — read tier (get_node/get_children/search/traverse/"
            "list_types/get_schema/find_path/history/diff/get_asset) and additive tier "
            "(create_node/update_node/link/propose_edges). You can only grow this graph: "
            "every write lands as a proposal for human review. Reviewing (accept/reject) "
            "and curative operations are not available over MCP — they belong to the "
            "human. Assets are served as small derived renditions — never the original "
            "binary (design §5.7)."
        ),
    )

    # ── Read tier ─────────────────────────────────────────────────────────

    @server.tool(annotations=_READ)
    def get_node(id: str, depth: int = 1) -> dict[str, Any]:
        """Fetch a node plus its active-edge neighborhood out to `depth` hops (0 = node alone)."""
        return _dump(service.get_neighborhood(id, depth=depth, principal=principal, path=db_path))

    @server.tool(annotations=_READ)
    def get_children(id: str) -> list[dict[str, Any]]:
        """List a node's children in position order (the document tree)."""
        return _dump(service.list_children(id, principal=principal, path=db_path))

    @server.tool(annotations=_READ)
    def search(
        query: str,
        k: int = 10,
        filters: dict[str, Any] | None = None,
        expand: bool = False,
    ) -> dict[str, Any]:
        """Hybrid search over node title + content: BM25 + vector, RRF-fused.

        The `vector` signal participates when an embedding provider is
        available on the server; otherwise results are BM25-only (no error).
        `signals` on each hit names the contributing signals.

        `filters` keys: `type`, `state` (default "active"; "any" for all),
        `created_by`, `created_after`, `created_before`. `expand` appends
        one-hop neighbors of the hits along active edges (graph signal).
        """
        filters = dict(filters or {})
        known = {"type", "state", "created_by", "created_after", "created_before"}
        unknown = sorted(set(filters) - known)
        if unknown:
            raise ValueError(f"unknown search filter(s): {', '.join(unknown)}")
        state = filters.pop("state", "active")
        result = search_module.search(
            query,
            k=k,
            state=None if state in (None, "any") else str(state),
            type=filters.pop("type", None),
            created_by=filters.pop("created_by", None),
            created_after=filters.pop("created_after", None),
            created_before=filters.pop("created_before", None),
            expand=expand,
            principal=principal,
            path=db_path,
        )
        return _dump(result)

    @server.tool(annotations=_READ)
    def traverse(
        start_id: str,
        edge_types: list[str] | None = None,
        depth: int = 2,
        direction: str = "both",
    ) -> dict[str, Any]:
        """Walk the subgraph reachable from `start_id` over active edges.

        `edge_types` restricts the walk (ids or names), `depth` caps hops,
        `direction` is "out" / "in" / "both".
        """
        return _dump(
            service.traverse(
                start_id,
                edge_types=edge_types,
                depth=depth,
                direction=direction,
                principal=principal,
                path=db_path,
            )
        )

    @server.tool(annotations=_READ)
    def list_types() -> dict[str, Any]:
        """List the full type catalog (node types and edge types)."""
        return _dump(service.list_types(principal=principal, path=db_path))

    @server.tool(annotations=_READ)
    def get_schema(type: str) -> dict[str, Any]:
        """Fetch one node or edge type's catalog entry (id or name), incl. its JSON schema."""
        return _dump(service.get_schema(type, principal=principal, path=db_path))

    @server.tool(annotations=_READ)
    def find_path(a: str, b: str) -> dict[str, Any]:
        """Find the shortest path between two nodes over active edges (any type)."""
        return _dump(service.find_path(a, b, principal=principal, path=db_path))

    @server.tool(annotations=_READ)
    def history(node_id: str) -> list[dict[str, Any]]:
        """List a node's version history (applied snapshots and proposed/rejected updates)."""
        return _dump(service.history(node_id, principal=principal, path=db_path))

    @server.tool(annotations=_READ)
    def diff(a: int, b: int) -> dict[str, Any]:
        """Unified diff between two versions of one node (ids from `history`)."""
        return _dump(service.diff_versions(a, b, principal=principal, path=db_path))

    @server.tool(annotations=_READ, structured_output=False)
    def get_asset(id_or_hash: str, rendition: str = "preview") -> list[Any]:
        """Fetch asset metadata plus a small derived rendition — NEVER the original.

        Design §5.7 binary policy: LLMs receive derived representations only.
        For images the result is a metadata text block followed by a WebP
        image block of the requested rendition (`preview` ≤1024px, the MCP
        default for vision models; `thumb` ≤256px). For non-image assets only
        the metadata block is returned (extracted text lands with the Phase-4
        ingestion pipeline). `full` originals are never served over MCP.
        """
        if rendition not in assets.PROFILES:
            raise ValueError(
                f"unsupported rendition {rendition!r}: MCP serves "
                f"{', '.join(sorted(assets.PROFILES))} only — originals never"
            )
        asset = assets.get_asset(id_or_hash, principal=principal, path=db_path)
        metadata: dict[str, Any] = {"asset": asset.model_dump(mode="json")}
        try:
            rend = assets.get_rendition(
                id_or_hash,
                profile=rendition,
                include_data=True,
                principal=principal,
                path=db_path,
            )
        except assets.UnsupportedRendition:
            # Not a renderable image: metadata (+ extracted text) only, per §5.7.
            metadata["rendition"] = None
            return [metadata]
        metadata["rendition"] = rend.model_dump(mode="json", exclude={"data_base64"})
        return [metadata, Image(data=assets.read_rendition_bytes(rend), format="webp")]

    # ── Additive tier ─────────────────────────────────────────────────────

    @server.tool(annotations=_ADDITIVE)
    def create_node(
        type: str,
        title: str | None = None,
        content: str = "",
        parent: str | None = None,
        props: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a node. Where it lands depends on your grant on the space.

        With a `suggest` grant the node is `proposed` and waits for review;
        with `edit` it lands `active` immediately — the grant is the whole
        difference, and nothing on this surface reports which you hold.

        Any `[[wikilinks]]` in `content` materialise as `mentions` edges in
        the same way, and a link into a space you may only suggest in stays
        `proposed` even when the node itself is live.
        """
        return _dump(
            service.create_node(
                type=type,
                title=title,
                content=content,
                parent_id=parent,
                props=props,
                principal=principal,
                path=db_path,
            )
        )

    @server.tool(annotations=_OVERWRITING)
    def update_node(
        id: str,
        title: str | None = None,
        content: str | None = None,
        props: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update a node — staged as a proposal, or applied in place under `edit`.

        With a `suggest` grant this stages a new `proposed` version (design
        §8.1): only the given fields are recorded, and accepting applies just
        those to the node as it stands then, so anything edited while your
        proposal waited is preserved. The node itself is untouched until a
        reviewer accepts — there is no tool on this surface that can accept.

        **With an `edit` grant the node is overwritten immediately**, its old
        content replaced and the mentions that content carried retired. That
        is why this tool is annotated destructive while the others are not.
        """
        kwargs: dict[str, Any] = {}
        if title is not None:
            kwargs["title"] = title
        if content is not None:
            kwargs["content"] = content
        if props is not None:
            kwargs["props"] = props
        return _dump(service.update_node(id, principal=principal, path=db_path, **kwargs))

    @server.tool(annotations=_ADDITIVE)
    def link(
        src: str,
        dst: str,
        edge_type: str,
        props: dict[str, Any] | None = None,
        confidence: float | None = None,
    ) -> dict[str, Any]:
        """Create a typed, directed edge; `proposed` under `suggest`, live under `edit`.

        The landing state needs the matching grant on **both** endpoint
        spaces: `edit` on one and `suggest` on the other stages the edge.

        `confidence` is your own estimate and is recorded as such — it is
        indicative data for the reviewer and triggers nothing on its own.
        """
        return _dump(
            service.create_edge(
                src,
                dst,
                edge_type,
                props=props,
                confidence=confidence,
                principal=principal,
                path=db_path,
            )
        )

    @server.tool(annotations=_ADDITIVE)
    def propose_edges(suggestions: list[dict[str, Any]]) -> dict[str, Any]:
        """Write a batch of edges: each suggestion is {src, dst, edge_type, props?, confidence?}.

        Each edge lands exactly as `link` would — `proposed` under `suggest`,
        live under `edit` on both endpoint spaces. Bad suggestions are
        reported in `failed` by index; the rest still write.
        """
        return _dump(service.propose_edges(suggestions, principal=principal, path=db_path))

    # ── Review tier (§8.1 "write (human)") is deliberately absent ──
    # `accept`/`reject` are not registered here: accepting makes proposed
    # structure live and archives what it replaces, which is destructive and
    # the human's call. The human works the queue through `nodum review …`.

    return server


def serve(*, token: str | None = None, db_path: str | Path | None = None) -> None:
    """Run the MCP server on stdio (blocking) — what MCP clients launch.

    The token comes from the environment, never the command line (a flag
    would leak into ``ps`` and shell history): ``NODUM_AGENT_TOKEN``, which
    is exactly the shape MCP client configs carry in their ``env`` blocks.

    Raises:
        ValueError: If the environment carries no token.
        auth.InvalidCredentials: If the token verifies no enabled agent.
    """
    token = token or os.environ.get("NODUM_AGENT_TOKEN")
    if not token:
        raise ValueError(
            "NODUM_AGENT_TOKEN is not set: the MCP server authenticates with an "
            "agent token (mint one with 'nodum agent create <name>')"
        )
    create_server(token=token, db_path=db_path).run()
