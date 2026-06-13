"use strict";

// Minimal, dependency-free client for the nodum read view. Every value coming
// from the API is injected with textContent (never innerHTML), so node text and
// payloads can contain arbitrary characters without breaking the DOM.

// ── DOM helpers ─────────────────────────────────────────────────────────────

/** Create an element with optional class, text, and child nodes. */
function el(tag, { cls, text, children } = {}) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (children) for (const child of children) node.appendChild(child);
  return node;
}

/** Replace all children of a container with the given nodes. */
function setChildren(container, nodes) {
  container.replaceChildren(...nodes);
}

/** First 8 characters of a UUID, for compact display. */
function shortUuid(uuid) {
  return String(uuid).slice(0, 8);
}

/** A node payload always has a `text` field; fall back gracefully. */
function nodeText(data) {
  return data && typeof data.text === "string" ? data.text : "(no text)";
}

// ── API calls (same-origin, relative URLs) ──────────────────────────────────

/** GET a relative URL and parse JSON, raising on a non-2xx response. */
async function fetchJson(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.json();
}

// ── Search ──────────────────────────────────────────────────────────────────

const searchForm = document.getElementById("search-form");
const searchInput = document.getElementById("search-input");
const searchLimit = document.getElementById("search-limit");
const searchStatus = document.getElementById("search-status");
const searchResults = document.getElementById("search-results");

searchForm.addEventListener("submit", (event) => {
  event.preventDefault();
  runSearch();
});

/** Run a full-text search and render the ranked hits. */
async function runSearch() {
  const query = searchInput.value.trim();
  if (!query) return;
  const limit = searchLimit.value || "20";
  searchStatus.textContent = "Searching…";
  setChildren(searchResults, []);
  try {
    const params = new URLSearchParams({ q: query, limit });
    const result = await fetchJson(`/search?${params.toString()}`);
    renderHits(result);
  } catch (error) {
    searchStatus.textContent = `Search failed: ${error.message}`;
  }
}

/** Render a SearchResult: a count line plus a clickable list of hits. */
function renderHits(result) {
  searchStatus.textContent = `${result.total} hit(s) for “${result.query}”`;
  const items = result.hits.map((hit) => {
    const title = el("span", { cls: "hit-text", text: nodeText(hit.data) });
    const meta = el("span", { cls: "hit-meta" });
    if (hit.data && hit.data.type) {
      meta.appendChild(el("span", { cls: "tag", text: hit.data.type }));
    }
    meta.appendChild(el("span", { cls: "score", text: `score ${hit.score.toFixed(4)}` }));
    meta.appendChild(el("span", { cls: "uuid", text: shortUuid(hit.uuid) }));

    const item = el("li", { cls: "hit", children: [title, meta] });
    item.tabIndex = 0;
    item.addEventListener("click", () => openNode(hit.uuid));
    item.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openNode(hit.uuid);
      }
    });
    return item;
  });
  setChildren(searchResults, items);
}

// ── Open node ───────────────────────────────────────────────────────────────

const nodeSection = document.getElementById("node-section");
const nodeStatus = document.getElementById("node-status");
const nodeDetail = document.getElementById("node-detail");
const expandControls = document.getElementById("expand-controls");
const expandButton = document.getElementById("expand-button");
const expandDepth = document.getElementById("expand-depth");
const expandStatus = document.getElementById("expand-status");
const expandDetail = document.getElementById("expand-detail");

let currentUuid = null;

/** Fetch one node with its incident edges and render the detail view. */
async function openNode(uuid) {
  currentUuid = uuid;
  nodeSection.hidden = false;
  nodeStatus.textContent = "Loading node…";
  setChildren(nodeDetail, []);
  setChildren(expandDetail, []);
  expandStatus.textContent = "";
  expandControls.hidden = true;
  nodeSection.scrollIntoView({ behavior: "smooth", block: "start" });
  try {
    const result = await fetchJson(`/nodes/${encodeURIComponent(uuid)}`);
    renderNode(result);
    expandControls.hidden = false;
  } catch (error) {
    nodeStatus.textContent = `Could not load node: ${error.message}`;
  }
}

/** Render a NodeWithEdges: text, full payload, and an edge table. */
function renderNode(result) {
  const node = result.node;
  nodeStatus.textContent = "";

  const blocks = [];
  blocks.push(el("p", { cls: "node-text", text: nodeText(node.data) }));

  const ids = el("p", { cls: "node-ids" });
  ids.appendChild(el("span", { cls: "uuid", text: node.uuid }));
  if (node.data && node.data.type) {
    ids.appendChild(el("span", { cls: "tag", text: node.data.type }));
  }
  blocks.push(ids);

  blocks.push(el("h3", { text: "Payload" }));
  blocks.push(el("pre", { cls: "payload", text: JSON.stringify(node.data, null, 2) }));

  blocks.push(el("h3", { text: `Edges (${result.edges.length})` }));
  blocks.push(renderEdgeTable(result.edges, node.uuid));

  setChildren(nodeDetail, blocks);
}

/** Build a table of incident edges: direction, type, and the other endpoint. */
function renderEdgeTable(edges, selfUuid) {
  if (edges.length === 0) {
    return el("p", { cls: "muted", text: "No incident edges." });
  }
  const header = el("tr", {
    children: [
      el("th", { text: "dir" }),
      el("th", { text: "type" }),
      el("th", { text: "from" }),
      el("th", { text: "to" }),
    ],
  });
  const rows = edges.map((edge) => {
    const outgoing = String(edge.from_uuid) === String(selfUuid);
    const type = (edge.data && edge.data.type) || "—";
    const fromCell = el("td", { cls: "uuid clickable", text: shortUuid(edge.from_uuid) });
    const toCell = el("td", { cls: "uuid clickable", text: shortUuid(edge.to_uuid) });
    fromCell.addEventListener("click", () => openNode(edge.from_uuid));
    toCell.addEventListener("click", () => openNode(edge.to_uuid));
    return el("tr", {
      children: [
        el("td", { cls: "dir", text: outgoing ? "out →" : "← in" }),
        el("td", { cls: "tag-cell", text: type }),
        fromCell,
        toCell,
      ],
    });
  });
  const table = el("table", { cls: "edges" });
  table.appendChild(el("thead", { children: [header] }));
  table.appendChild(el("tbody", { children: rows }));
  return table;
}

// ── Expand ──────────────────────────────────────────────────────────────────

expandButton.addEventListener("click", () => runExpand());

/** Expand the current node's subgraph and render its nodes + edges. */
async function runExpand() {
  if (!currentUuid) return;
  const depth = expandDepth.value || "1";
  expandStatus.textContent = "Expanding…";
  setChildren(expandDetail, []);
  try {
    const params = new URLSearchParams({ seed: currentUuid, depth });
    const result = await fetchJson(`/expand?${params.toString()}`);
    renderSubgraph(result);
  } catch (error) {
    expandStatus.textContent = `Expand failed: ${error.message}`;
  }
}

/** Render a Subgraph: a node list and an edge list (from —type→ to). */
function renderSubgraph(result) {
  expandStatus.textContent = `depth ${result.depth}: ${result.nodes.length} node(s), ${result.edges.length} edge(s)`;

  // Map uuid → text so edges can show readable endpoints.
  const textByUuid = new Map();
  for (const node of result.nodes) {
    textByUuid.set(String(node.uuid), nodeText(node.data));
  }
  const label = (uuid) => textByUuid.get(String(uuid)) || shortUuid(uuid);

  const blocks = [];

  blocks.push(el("h3", { text: "Nodes" }));
  const nodeItems = result.nodes.map((node) => {
    const text = el("span", { cls: "hit-text", text: nodeText(node.data) });
    const meta = el("span", { cls: "hit-meta" });
    if (node.data && node.data.type) {
      meta.appendChild(el("span", { cls: "tag", text: node.data.type }));
    }
    meta.appendChild(el("span", { cls: "uuid", text: shortUuid(node.uuid) }));
    const item = el("li", { cls: "hit", children: [text, meta] });
    item.tabIndex = 0;
    item.addEventListener("click", () => openNode(node.uuid));
    item.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openNode(node.uuid);
      }
    });
    return item;
  });
  blocks.push(el("ul", { cls: "results", children: nodeItems }));

  blocks.push(el("h3", { text: "Edges" }));
  if (result.edges.length === 0) {
    blocks.push(el("p", { cls: "muted", text: "No edges in this subgraph." }));
  } else {
    const edgeItems = result.edges.map((edge) => {
      const type = (edge.data && edge.data.type) || "—";
      return el("li", {
        cls: "edge-line",
        children: [
          el("span", { cls: "endpoint", text: label(edge.from_uuid) }),
          el("span", { cls: "arrow", text: ` —${type}→ ` }),
          el("span", { cls: "endpoint", text: label(edge.to_uuid) }),
        ],
      });
    });
    blocks.push(el("ul", { cls: "edge-list", children: edgeItems }));
  }

  setChildren(expandDetail, blocks);
}
