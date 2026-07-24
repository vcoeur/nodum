"""The MCP server adapter (design §8) — a thin FastMCP front over the service layer.

Exposes the design §8.1 v1 tool contract's **read tier** (``get_node``,
``get_children``, ``search``, ``traverse``, ``list_types``, ``get_schema``,
``find_path``, ``history``, ``diff``, ``get_asset``), **additive tier**
(``create_node``, ``update_node``, ``link``, ``propose_edges``), and the
``accept``/``reject`` review tools (the §8.1 "write" tier — they cost nothing
on top of the review API and let a human-driven MCP client work the queue).

``get_asset`` enforces the §5.7 binary policy structurally: agents receive
metadata plus a small derived rendition (``preview``/``thumb`` WebP image
block); original binaries are never served over MCP.

**Curative tools are never registered** (``merge_nodes``, ``retype``,
``supersede_edge``, ``bulk_relink``, ``consolidate``) — structural
enforcement of §8.2: they simply do not exist on this surface.

Identity: one configured actor per server (``nodum mcp serve --actor``), so
every write is attributed to the connecting agent and lands ``proposed``
unless the agent's stored policy auto-accepts it. Transport is stdio — what
MCP clients actually launch.

Every tool delegates to :mod:`nodum.service` / :mod:`nodum.search`; there is
no logic here beyond argument mapping and JSON shaping.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from nodum import assets, service
from nodum import search as search_module

#: Tool annotations per tier (design §8): reads are read-only; additive
#: writes are non-destructive (they only ever *propose* new state — even an
#: auto-accepted write is additive, and everything is reversible via undo).
_READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)

#: Curative tools (design §8.2) — asserted absent from the registry in tests.
CURATIVE_TOOLS = ("merge_nodes", "retype", "supersede_edge", "bulk_relink", "consolidate")

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
REVIEW_TOOLS = ("accept", "reject")


def _dump(result: BaseModel | list[BaseModel]) -> dict[str, Any] | list[dict[str, Any]]:
    """Serialise service results exactly like every other adapter."""
    if isinstance(result, list):
        return [item.model_dump(mode="json") for item in result]
    return result.model_dump(mode="json")


def create_server(*, actor: str = "agent:mcp", db_path: str | Path | None = None) -> FastMCP:
    """Build the nodum MCP server bound to one agent identity and database.

    Args:
        actor: The actor string every write is attributed to (e.g.
            ``agent:researcher``). Non-``human`` actors land writes in
            ``proposed`` unless their stored policy auto-accepts.
        db_path: Explicit database path; defaults to ``NODUM_DB`` resolution.

    Returns:
        A FastMCP server with the read, additive, and review tools
        registered (curative tools are never registered — design §8.2).
    """
    server = FastMCP(
        "nodum",
        instructions=(
            "nodum knowledge graph — read tier (get_node/get_children/search/traverse/"
            "list_types/get_schema/find_path/history/diff/get_asset), additive tier "
            "(create_node/update_node/link/propose_edges), and review tools "
            "(accept/reject). Every write lands as a proposal for human review unless "
            "your stored policy auto-accepts it. Curative operations are not available "
            "over MCP. Assets are served as small derived renditions — never the "
            "original binary (design §5.7)."
        ),
    )

    # ── Read tier ─────────────────────────────────────────────────────────

    @server.tool(annotations=_READ)
    def get_node(id: str, depth: int = 1) -> dict[str, Any]:
        """Fetch a node plus its active-edge neighborhood out to `depth` hops (0 = node alone)."""
        return _dump(service.get_neighborhood(id, depth=depth, path=db_path))

    @server.tool(annotations=_READ)
    def get_children(id: str) -> list[dict[str, Any]]:
        """List a node's children in position order (the document tree)."""
        return _dump(service.list_children(id, path=db_path))

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
                start_id, edge_types=edge_types, depth=depth, direction=direction, path=db_path
            )
        )

    @server.tool(annotations=_READ)
    def list_types() -> dict[str, Any]:
        """List the full type catalog (node types and edge types)."""
        return _dump(service.list_types(path=db_path))

    @server.tool(annotations=_READ)
    def get_schema(type: str) -> dict[str, Any]:
        """Fetch one node or edge type's catalog entry (id or name), incl. its JSON schema."""
        return _dump(service.get_schema(type, path=db_path))

    @server.tool(annotations=_READ)
    def find_path(a: str, b: str) -> dict[str, Any]:
        """Find the shortest path between two nodes over active edges (any type)."""
        return _dump(service.find_path(a, b, path=db_path))

    @server.tool(annotations=_READ)
    def history(node_id: str) -> list[dict[str, Any]]:
        """List a node's version history (applied snapshots and proposed/rejected updates)."""
        return _dump(service.history(node_id, path=db_path))

    @server.tool(annotations=_READ)
    def diff(a: int, b: int) -> dict[str, Any]:
        """Unified diff between two versions of one node (ids from `history`)."""
        return _dump(service.diff_versions(a, b, path=db_path))

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
        asset = assets.get_asset(id_or_hash, path=db_path)
        metadata: dict[str, Any] = {"asset": asset.model_dump(mode="json")}
        try:
            rend = assets.get_rendition(
                id_or_hash, profile=rendition, include_data=True, path=db_path
            )
        except assets.UnsupportedRendition:
            # Not a renderable image: metadata (+ extracted text) only, per §5.7.
            metadata["rendition"] = None
            return [metadata]
        metadata["rendition"] = rend.model_dump(mode="json", exclude={"data_base64"})
        return [metadata, Image(data=assets.read_rendition_bytes(rend), format="webp")]

    # ── Additive tier ─────────────────────────────────────────────────────

    @server.tool(annotations=_WRITE)
    def create_node(
        type: str,
        title: str | None = None,
        content: str = "",
        parent: str | None = None,
        props: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a node. Lands `proposed` for agent actors (active only for human/policy)."""
        return _dump(
            service.create_node(
                type=type,
                title=title,
                content=content,
                parent_id=parent,
                props=props,
                actor=actor,
                path=db_path,
            )
        )

    @server.tool(annotations=_WRITE)
    def update_node(
        id: str,
        title: str | None = None,
        content: str | None = None,
        props: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Propose an update to a node: stages a new `proposed` version (design §8.1).

        Only the given fields change. The node itself is untouched until a
        reviewer accepts the proposed version.
        """
        kwargs: dict[str, Any] = {}
        if title is not None:
            kwargs["title"] = title
        if content is not None:
            kwargs["content"] = content
        if props is not None:
            kwargs["props"] = props
        return _dump(service.update_node(id, actor=actor, path=db_path, **kwargs))

    @server.tool(annotations=_WRITE)
    def link(
        src: str,
        dst: str,
        edge_type: str,
        props: dict[str, Any] | None = None,
        confidence: float | None = None,
    ) -> dict[str, Any]:
        """Create a typed, directed edge. Lands `proposed` unless policy auto-accepts it."""
        return _dump(
            service.create_edge(
                src, dst, edge_type, props=props, confidence=confidence, actor=actor, path=db_path
            )
        )

    @server.tool(annotations=_WRITE)
    def propose_edges(suggestions: list[dict[str, Any]]) -> dict[str, Any]:
        """Propose a batch of edges: each suggestion is {src, dst, edge_type, props?, confidence?}.

        Bad suggestions are reported in `failed` by index; the rest still write.
        """
        return _dump(service.propose_edges(suggestions, actor=actor, path=db_path))

    # ── Review tools (§8.1 "write" tier) ──────────────────────────────────

    @server.tool(annotations=_WRITE)
    def accept(ids: list[str]) -> dict[str, Any]:
        """Accept proposals by id (node/edge ids, or version ids for proposed updates)."""
        return _dump(service.accept_proposals(ids, actor=actor, path=db_path))

    @server.tool(annotations=_WRITE)
    def reject(ids: list[str], reason: str) -> dict[str, Any]:
        """Reject proposals by id, recording `reason` in every reject event."""
        return _dump(service.reject_proposals(ids, reason=reason, actor=actor, path=db_path))

    return server


def serve(*, actor: str = "agent:mcp", db_path: str | Path | None = None) -> None:
    """Run the MCP server on stdio (blocking) — what MCP clients launch."""
    create_server(actor=actor, db_path=db_path).run()
