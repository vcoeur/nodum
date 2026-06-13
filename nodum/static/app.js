"use strict";

// Schema-driven, dependency-free client for the nodum typed-graph view. Every
// value coming from the API is injected with textContent (or SVG createElementNS
// + textContent), never innerHTML, so node text and payloads may contain
// arbitrary characters without breaking the DOM or enabling injection.

// ── Module state ─────────────────────────────────────────────────────────────

let schemaCache = null;
const nodeKindByName = new Map(); // name -> {name, group, text_label, fields}
const edgeKindByName = new Map(); // name -> {name, from, to, symmetric, fields}
const kindToGroup = new Map(); // node-kind name -> group
const nodeCache = new Map(); // uuid -> {text, kind} (for endpoint labels)

let currentUuid = null;
let fromPicker = null;
let toPicker = null;

// ── DOM helpers ──────────────────────────────────────────────────────────────

/** Create an HTML element with optional class, text, and child nodes. */
function el(tag, { cls, text, children } = {}) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (children) for (const child of children) node.appendChild(child);
  return node;
}

const SVG_NS = "http://www.w3.org/2000/svg";

/** Create an SVG element and set string attributes (namespaced). */
function svgEl(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
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

/** A node payload always carries a `text` field; fall back gracefully. */
function nodeText(data) {
  return data && typeof data.text === "string" ? data.text : "(no text)";
}

/** Truncate a string to at most n characters, with an ellipsis. */
function truncate(text, n) {
  const s = String(text);
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

// ── API calls (same-origin, relative URLs) ───────────────────────────────────

/** Issue a fetch and parse JSON, surfacing the API's `detail` on any non-2xx. */
async function request(method, url, body) {
  const options = { method, headers: { Accept: "application/json" } };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(url, options);
  const raw = await response.text();
  let payload = null;
  if (raw) {
    try {
      payload = JSON.parse(raw);
    } catch {
      payload = null;
    }
  }
  if (!response.ok) {
    const detail = payload && payload.detail ? payload.detail : `${response.status} ${response.statusText}`;
    throw new Error(detail);
  }
  return payload;
}

const apiGet = (url) => request("GET", url);
const apiSend = (method, url, body) => request(method, url, body);

// ── Schema-driven form fields ────────────────────────────────────────────────

/** Build one labelled control for a FieldSpec, prefilled from `value`. */
function buildOneField(name, spec, value) {
  const label = el("label", { cls: "field" });
  label.appendChild(el("span", { cls: "field-name", text: spec.required ? `${name} *` : name }));

  let control;
  switch (spec.type) {
    case "bool":
      control = el("input");
      control.type = "checkbox";
      control.checked = Boolean(value);
      break;
    case "int":
      control = el("input");
      control.type = "number";
      control.step = "1";
      if (value !== undefined && value !== null) control.value = String(value);
      break;
    case "float":
      control = el("input");
      control.type = "number";
      control.step = "any";
      if (value !== undefined && value !== null) control.value = String(value);
      break;
    case "enum": {
      control = el("select");
      const blank = el("option", { text: "(none)" });
      blank.value = "";
      control.appendChild(blank);
      for (const choice of spec.choices || []) {
        const option = el("option", { text: choice });
        option.value = choice;
        control.appendChild(option);
      }
      if (value !== undefined && value !== null) control.value = String(value);
      break;
    }
    case "list[str]":
      control = el("input");
      control.type = "text";
      control.placeholder = "comma, separated";
      if (Array.isArray(value)) control.value = value.join(", ");
      break;
    default: // str and any unknown type
      control = el("input");
      control.type = "text";
      if (value !== undefined && value !== null) control.value = String(value);
  }

  label.appendChild(control);
  if (spec.description) label.appendChild(el("span", { cls: "field-desc", text: spec.description }));
  return { wrapper: label, control };
}

/** Render every field of a kind into `container`; stash descriptors for reading. */
function buildFieldInputs(fields, container, values = {}) {
  const descriptors = [];
  const wrappers = [];
  for (const [name, spec] of Object.entries(fields || {})) {
    const { wrapper, control } = buildOneField(name, spec, values[name]);
    descriptors.push({ name, spec, control });
    wrappers.push(wrapper);
  }
  container.fieldControls = descriptors;
  setChildren(container, wrappers);
}

/** Read one control, coercing to its declared type; report whether it is empty. */
function readControl(control, spec) {
  if (spec.type === "bool") {
    return { value: control.checked, empty: !control.checked };
  }
  const raw = control.value;
  if (spec.type === "int" || spec.type === "float") {
    const trimmed = raw.trim();
    if (trimmed === "") return { value: null, empty: true };
    const num = spec.type === "int" ? parseInt(trimmed, 10) : parseFloat(trimmed);
    if (Number.isNaN(num)) return { value: null, empty: true };
    return { value: num, empty: false };
  }
  if (spec.type === "list[str]") {
    const items = raw
      .split(",")
      .map((item) => item.trim())
      .filter((item) => item.length > 0);
    return { value: items, empty: items.length === 0 };
  }
  const trimmed = raw.trim(); // enum, str, unknown
  return { value: trimmed, empty: trimmed === "" };
}

/**
 * Collect a `data` object from a field container.
 * Create mode omits empty optional fields; patch mode (`clearEmpties`) sends
 * `null` for emptied fields so the merge clears them.
 */
function collectFieldData(container, { clearEmpties = false } = {}) {
  const data = {};
  for (const { name, spec, control } of container.fieldControls || []) {
    const { value, empty } = readControl(control, spec);
    if (empty) {
      if (clearEmpties) data[name] = null;
      else if (spec.required) data[name] = value;
    } else {
      data[name] = value;
    }
  }
  return data;
}

// ── Schema load + select population ──────────────────────────────────────────

const schemaStatus = document.getElementById("schema-status");

/** Fetch the metamodel, index it, and populate every kind-driven control. */
async function loadSchema() {
  try {
    schemaCache = await apiGet("/schema");
  } catch (error) {
    schemaStatus.textContent = `Failed to load schema: ${error.message}`;
    return;
  }
  for (const nk of schemaCache.node_kinds) {
    nodeKindByName.set(nk.name, nk);
    kindToGroup.set(nk.name, nk.group);
  }
  for (const ek of schemaCache.edge_kinds) edgeKindByName.set(ek.name, ek);

  populateSearchKinds();
  populateCreateNodeKinds();
  populateCreateEdgeKinds();

  schemaStatus.textContent =
    `Loaded ${schemaCache.node_kinds.length} node kinds, ` +
    `${schemaCache.edge_kinds.length} edge kinds.`;
}

/** Fill an existing <select> with options; first option is an optional blank. */
function fillSelect(select, names, blankLabel) {
  const options = [];
  if (blankLabel !== undefined) {
    const blank = el("option", { text: blankLabel });
    blank.value = "";
    options.push(blank);
  }
  for (const name of names) {
    const option = el("option", { text: name });
    option.value = name;
    options.push(option);
  }
  setChildren(select, options);
}

// ── Search ───────────────────────────────────────────────────────────────────

const searchForm = document.getElementById("search-form");
const searchInput = document.getElementById("search-input");
const searchKind = document.getElementById("search-kind");
const searchLimit = document.getElementById("search-limit");
const searchStatus = document.getElementById("search-status");
const searchResults = document.getElementById("search-results");

function populateSearchKinds() {
  fillSelect(searchKind, [...nodeKindByName.keys()], "(any kind)");
}

searchForm.addEventListener("submit", (event) => {
  event.preventDefault();
  runSearch();
});

/** Run a full-text search (optionally kind-filtered) and render ranked hits. */
async function runSearch() {
  const query = searchInput.value.trim();
  if (!query) {
    searchStatus.textContent = "Enter a query.";
    return;
  }
  const params = new URLSearchParams({ q: query, limit: searchLimit.value || "20" });
  if (searchKind.value) params.set("kind", searchKind.value);
  searchStatus.textContent = "Searching…";
  setChildren(searchResults, []);
  try {
    const result = await apiGet(`/search?${params.toString()}`);
    renderHits(result);
  } catch (error) {
    searchStatus.textContent = `Search failed: ${error.message}`;
  }
}

/** Build a clickable list item for a node-shaped record (hit or neighbour). */
function nodeListItem(record) {
  nodeCache.set(String(record.uuid), { text: nodeText(record.data), kind: record.kind });
  const title = el("span", { cls: "hit-text", text: nodeText(record.data) });
  const meta = el("span", { cls: "hit-meta" });
  meta.appendChild(el("span", { cls: "tag", text: record.kind }));
  if (typeof record.score === "number") {
    meta.appendChild(el("span", { cls: "score", text: `score ${record.score.toFixed(4)}` }));
  }
  meta.appendChild(el("span", { cls: "uuid", text: shortUuid(record.uuid) }));

  const item = el("li", { cls: "hit", children: [title, meta] });
  item.tabIndex = 0;
  const open = () => openNode(record.uuid);
  item.addEventListener("click", open);
  item.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      open();
    }
  });
  return item;
}

/** Render a SearchResult: a count line plus a clickable list of hits. */
function renderHits(result) {
  searchStatus.textContent = `${result.total} hit(s) for “${result.query}”`;
  setChildren(searchResults, result.hits.map(nodeListItem));
}

// ── Create node ──────────────────────────────────────────────────────────────

const createNodeForm = document.getElementById("create-node-form");
const createNodeKind = document.getElementById("create-node-kind");
const createNodeTextName = document.getElementById("create-node-text-name");
const createNodeText = document.getElementById("create-node-text");
const createNodeFields = document.getElementById("create-node-fields");
const createNodeStatus = document.getElementById("create-node-status");

function populateCreateNodeKinds() {
  fillSelect(createNodeKind, [...nodeKindByName.keys()]);
  createNodeKind.addEventListener("change", () => renderCreateNodeFields(createNodeKind.value));
  renderCreateNodeFields(createNodeKind.value);
}

/** Re-render the create-node field set (and the text label) for a kind. */
function renderCreateNodeFields(kindName) {
  const nk = nodeKindByName.get(kindName);
  if (!nk) return;
  createNodeTextName.textContent = nk.text_label || "text";
  buildFieldInputs(nk.fields, createNodeFields);
}

createNodeForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const kindName = createNodeKind.value;
  const text = createNodeText.value.trim();
  if (!text) {
    createNodeStatus.textContent = "Text is required.";
    return;
  }
  const data = collectFieldData(createNodeFields);
  createNodeStatus.textContent = "Creating…";
  try {
    const node = await apiSend("POST", "/nodes", { kind: kindName, text, data });
    createNodeStatus.textContent = `Created ${node.kind} ${shortUuid(node.uuid)}.`;
    createNodeText.value = "";
    renderCreateNodeFields(kindName);
    openNode(node.uuid);
  } catch (error) {
    createNodeStatus.textContent = error.message;
  }
});

// ── Node picker (search-and-select, kind-filtered) ───────────────────────────

/**
 * Build a small search-by-text picker restricted to `allowedKinds`.
 * Returns the element to mount and `getSelected()` for the chosen uuid.
 */
function makePicker(allowedKinds) {
  const input = el("input");
  input.type = "search";
  input.placeholder = "type, then Find…";
  const findButton = el("button", { text: "Find" });
  findButton.type = "button";
  const results = el("ul", { cls: "picker-results" });
  const selected = el("div", { cls: "picker-selected muted", text: "none selected" });
  let selectedUuid = null;

  async function run() {
    const query = input.value.trim();
    if (!query) return;
    setChildren(results, [el("li", { cls: "muted", text: "Searching…" })]);
    try {
      const result = await apiGet(`/search?q=${encodeURIComponent(query)}&limit=20`);
      const hits = result.hits.filter((hit) => allowedKinds.includes(hit.kind));
      if (hits.length === 0) {
        setChildren(results, [el("li", { cls: "muted", text: "No matches in allowed kinds." })]);
        return;
      }
      setChildren(results, hits.map((hit) => pickerItem(hit)));
    } catch (error) {
      setChildren(results, [el("li", { cls: "muted", text: `Search failed: ${error.message}` })]);
    }
  }

  function pickerItem(hit) {
    const tag = el("span", { cls: "tag", text: hit.kind });
    const label = el("span", { cls: "hit-text", text: nodeText(hit.data) });
    const id = el("span", { cls: "uuid", text: shortUuid(hit.uuid) });
    const item = el("li", { cls: "picker-hit", children: [tag, label, id] });
    item.tabIndex = 0;
    const choose = () => {
      selectedUuid = hit.uuid;
      selected.className = "picker-selected";
      setChildren(selected, [
        el("span", { cls: "tag", text: hit.kind }),
        el("span", { cls: "hit-text", text: nodeText(hit.data) }),
        el("span", { cls: "uuid", text: shortUuid(hit.uuid) }),
      ]);
      setChildren(results, []);
    };
    item.addEventListener("click", choose);
    item.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        choose();
      }
    });
    return item;
  }

  findButton.addEventListener("click", run);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      run();
    }
  });

  const element = el("div", { cls: "picker" });
  element.appendChild(el("div", { cls: "picker-input-row", children: [input, findButton] }));
  element.appendChild(results);
  element.appendChild(selected);
  return { element, getSelected: () => selectedUuid };
}

// ── Create edge ──────────────────────────────────────────────────────────────

const createEdgeForm = document.getElementById("create-edge-form");
const createEdgeKind = document.getElementById("create-edge-kind");
const createEdgeSignature = document.getElementById("create-edge-signature");
const createEdgeFrom = document.getElementById("create-edge-from");
const createEdgeTo = document.getElementById("create-edge-to");
const createEdgeFields = document.getElementById("create-edge-fields");
const createEdgeStatus = document.getElementById("create-edge-status");

function populateCreateEdgeKinds() {
  fillSelect(createEdgeKind, [...edgeKindByName.keys()]);
  createEdgeKind.addEventListener("change", () => onEdgeKindChange(createEdgeKind.value));
  onEdgeKindChange(createEdgeKind.value);
}

/** Show the signature and rebuild both pickers + fields for an edge kind. */
function onEdgeKindChange(kindName) {
  const ek = edgeKindByName.get(kindName);
  if (!ek) return;
  setChildren(createEdgeSignature, [
    el("span", { cls: "sig-side", text: `from: ${ek.from.join(", ")}` }),
    el("span", { cls: "sig-arrow", text: " → " }),
    el("span", { cls: "sig-side", text: `to: ${ek.to.join(", ")}` }),
  ]);
  fromPicker = makePicker(ek.from);
  toPicker = makePicker(ek.to);
  setChildren(createEdgeFrom, [fromPicker.element]);
  setChildren(createEdgeTo, [toPicker.element]);
  buildFieldInputs(ek.fields, createEdgeFields);
}

createEdgeForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const kindName = createEdgeKind.value;
  const fromUuid = fromPicker && fromPicker.getSelected();
  const toUuid = toPicker && toPicker.getSelected();
  if (!fromUuid || !toUuid) {
    createEdgeStatus.textContent = "Pick both a 'from' and a 'to' node.";
    return;
  }
  const data = collectFieldData(createEdgeFields);
  createEdgeStatus.textContent = "Creating…";
  try {
    const edge = await apiSend("POST", "/edges", {
      kind: kindName,
      from_uuid: fromUuid,
      to_uuid: toUuid,
      data,
    });
    createEdgeStatus.textContent = `Created ${edge.kind} ${shortUuid(edge.uuid)}.`;
    onEdgeKindChange(kindName);
    openNode(fromUuid);
  } catch (error) {
    createEdgeStatus.textContent = error.message;
  }
});

// ── Node detail ──────────────────────────────────────────────────────────────

const nodeSection = document.getElementById("node-section");
const nodeStatus = document.getElementById("node-status");
const nodeActions = document.getElementById("node-actions");
const nodeEdit = document.getElementById("node-edit");
const nodeDetail = document.getElementById("node-detail");
const graphControls = document.getElementById("graph-controls");
const graphButton = document.getElementById("graph-button");
const graphDepth = document.getElementById("graph-depth");
const graphStatus = document.getElementById("graph-status");
const graphDetail = document.getElementById("graph-detail");

/** Ensure nodeCache has {text, kind} for each uuid, fetching the gaps. */
async function ensureNodeTexts(uuids) {
  const missing = [...new Set(uuids.map(String))].filter((uuid) => !nodeCache.has(uuid));
  await Promise.allSettled(
    missing.map(async (uuid) => {
      try {
        const result = await apiGet(`/nodes/${encodeURIComponent(uuid)}`);
        nodeCache.set(uuid, { text: nodeText(result.node.data), kind: result.node.kind });
      } catch {
        nodeCache.set(uuid, { text: shortUuid(uuid), kind: "?" });
      }
    }),
  );
}

/** Fetch one node with its incident edges and render the full detail view. */
async function openNode(uuid) {
  currentUuid = String(uuid);
  nodeSection.hidden = false;
  setChildren(nodeActions, []);
  setChildren(nodeEdit, []);
  setChildren(nodeDetail, []);
  graphControls.hidden = true;
  graphDetail.hidden = true;
  setChildren(graphDetail, []);
  graphStatus.textContent = "";
  nodeStatus.textContent = "Loading node…";
  nodeSection.scrollIntoView({ behavior: "smooth", block: "start" });
  try {
    const result = await apiGet(`/nodes/${encodeURIComponent(uuid)}`);
    nodeCache.set(String(result.node.uuid), {
      text: nodeText(result.node.data),
      kind: result.node.kind,
    });
    const others = result.edges.map((edge) =>
      String(edge.from_uuid) === currentUuid ? edge.to_uuid : edge.from_uuid,
    );
    await ensureNodeTexts(others);
    nodeStatus.textContent = "";
    renderActions(result);
    renderNode(result);
    graphControls.hidden = false;
  } catch (error) {
    nodeStatus.textContent = `Could not load node: ${error.message}`;
  }
}

/** Render the Edit / Delete action buttons for the open node. */
function renderActions(result) {
  const node = result.node;
  const edgeCount = result.edges.length;
  const editButton = el("button", { text: "Edit" });
  editButton.type = "button";
  editButton.addEventListener("click", () => openEditForm(node));

  const deleteButton = el("button", { cls: "danger", text: "Delete" });
  deleteButton.type = "button";
  deleteButton.addEventListener("click", () => deleteNode(node, edgeCount));

  setChildren(nodeActions, [editButton, deleteButton]);
}

/** Render a NodeWithEdges: text, identity, payload, and the edge table. */
function renderNode(result) {
  const node = result.node;
  const blocks = [];
  blocks.push(el("p", { cls: "node-text", text: nodeText(node.data) }));

  const ids = el("p", { cls: "node-ids" });
  ids.appendChild(el("span", { cls: "tag", text: node.kind }));
  ids.appendChild(el("span", { cls: "uuid", text: node.uuid }));
  blocks.push(ids);

  blocks.push(el("h3", { text: "Payload" }));
  blocks.push(el("pre", { cls: "payload", text: JSON.stringify(node.data, null, 2) }));

  blocks.push(el("h3", { text: `Edges (${result.edges.length})` }));
  blocks.push(renderEdgeTable(result.edges, node.uuid));

  setChildren(nodeDetail, blocks);
}

/** Build the incident-edge table: direction, kind, other endpoint, actions. */
function renderEdgeTable(edges, selfUuid) {
  if (edges.length === 0) {
    return el("p", { cls: "muted", text: "No incident edges." });
  }
  const header = el("tr", {
    children: [
      el("th", { text: "dir" }),
      el("th", { text: "kind" }),
      el("th", { text: "other endpoint" }),
      el("th", { text: "" }),
    ],
  });
  const rows = edges.map((edge) => {
    const outgoing = String(edge.from_uuid) === String(selfUuid);
    const otherUuid = outgoing ? edge.to_uuid : edge.from_uuid;
    const info = nodeCache.get(String(otherUuid)) || { text: shortUuid(otherUuid), kind: "?" };

    const otherCell = el("td", { cls: "other-cell" });
    const otherText = el("span", { cls: "endpoint clickable", text: info.text });
    otherText.tabIndex = 0;
    otherText.addEventListener("click", () => openNode(otherUuid));
    otherText.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openNode(otherUuid);
      }
    });
    otherCell.appendChild(el("span", { cls: "tag", text: info.kind }));
    otherCell.appendChild(otherText);

    const actionsCell = el("td", { cls: "edge-actions" });
    const ek = edgeKindByName.get(edge.kind);
    if (ek && ek.fields && Object.keys(ek.fields).length > 0) {
      const editEdge = el("button", { cls: "small", text: "edit" });
      editEdge.type = "button";
      editEdge.addEventListener("click", () => openEdgeEdit(edge));
      actionsCell.appendChild(editEdge);
    }
    const deleteEdge = el("button", { cls: "small danger", text: "delete" });
    deleteEdge.type = "button";
    deleteEdge.addEventListener("click", () => deleteEdgeRow(edge));
    actionsCell.appendChild(deleteEdge);

    return el("tr", {
      children: [
        el("td", { cls: "dir", text: outgoing ? "out →" : "← in" }),
        el("td", { cls: "tag-cell", text: edge.kind }),
        otherCell,
        actionsCell,
      ],
    });
  });
  const table = el("table", { cls: "edges" });
  table.appendChild(el("thead", { children: [header] }));
  table.appendChild(el("tbody", { children: rows }));
  return table;
}

// ── Edit node ────────────────────────────────────────────────────────────────

/** Open an inline edit form for a node, prefilled from its current payload. */
function openEditForm(node) {
  const nk = nodeKindByName.get(node.kind);
  const form = el("form", { cls: "edit-form" });
  form.appendChild(el("h3", { text: `Edit ${node.kind}` }));

  const textLabel = el("label", { cls: "block" });
  textLabel.appendChild(el("span", { text: (nk && nk.text_label) || "text" }));
  const textArea = el("textarea");
  textArea.rows = 2;
  textArea.value = nodeText(node.data);
  textLabel.appendChild(textArea);
  form.appendChild(textLabel);

  const fieldsContainer = el("div", { cls: "fields" });
  buildFieldInputs(nk ? nk.fields : {}, fieldsContainer, node.data);
  form.appendChild(fieldsContainer);

  const status = el("div", { cls: "status" });
  const save = el("button", { text: "Save" });
  save.type = "submit";
  const cancel = el("button", { cls: "ghost", text: "Cancel" });
  cancel.type = "button";
  cancel.addEventListener("click", () => setChildren(nodeEdit, []));
  form.appendChild(el("div", { cls: "actions", children: [save, cancel] }));
  form.appendChild(status);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = textArea.value.trim();
    if (!text) {
      status.textContent = "Text is required.";
      return;
    }
    const data = collectFieldData(fieldsContainer, { clearEmpties: true });
    status.textContent = "Saving…";
    try {
      await apiSend("PATCH", `/nodes/${encodeURIComponent(node.uuid)}`, { text, data });
      setChildren(nodeEdit, []);
      openNode(node.uuid);
    } catch (error) {
      status.textContent = error.message;
    }
  });

  setChildren(nodeEdit, [form]);
}

/** Open an inline edit form for an edge's payload fields. */
function openEdgeEdit(edge) {
  const ek = edgeKindByName.get(edge.kind);
  const form = el("form", { cls: "edit-form" });
  form.appendChild(el("h3", { text: `Edit ${edge.kind} edge` }));

  const fieldsContainer = el("div", { cls: "fields" });
  buildFieldInputs(ek ? ek.fields : {}, fieldsContainer, edge.data);
  form.appendChild(fieldsContainer);

  const status = el("div", { cls: "status" });
  const save = el("button", { text: "Save" });
  save.type = "submit";
  const cancel = el("button", { cls: "ghost", text: "Cancel" });
  cancel.type = "button";
  cancel.addEventListener("click", () => setChildren(nodeEdit, []));
  form.appendChild(el("div", { cls: "actions", children: [save, cancel] }));
  form.appendChild(status);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = collectFieldData(fieldsContainer, { clearEmpties: true });
    status.textContent = "Saving…";
    try {
      await apiSend("PATCH", `/edges/${encodeURIComponent(edge.uuid)}`, { data });
      setChildren(nodeEdit, []);
      openNode(currentUuid);
    } catch (error) {
      status.textContent = error.message;
    }
  });

  setChildren(nodeEdit, [form]);
}

// ── Delete ───────────────────────────────────────────────────────────────────

/** Confirm and delete a node (cascading its edges), then clear the detail view. */
async function deleteNode(node, edgeCount) {
  const message =
    `Delete this ${node.kind} node and its ${edgeCount} incident edge(s)? ` +
    "This cannot be undone.";
  if (!window.confirm(message)) return;
  try {
    const result = await apiSend("DELETE", `/nodes/${encodeURIComponent(node.uuid)}`);
    nodeCache.delete(String(node.uuid));
    setChildren(nodeActions, []);
    setChildren(nodeEdit, []);
    setChildren(nodeDetail, []);
    graphControls.hidden = true;
    graphDetail.hidden = true;
    setChildren(graphDetail, []);
    currentUuid = null;
    nodeStatus.textContent = `Deleted ${result.deleted} row(s) (node + cascaded edges).`;
  } catch (error) {
    nodeStatus.textContent = error.message;
  }
}

/** Confirm and delete a single edge, then refresh the open node. */
async function deleteEdgeRow(edge) {
  if (!window.confirm(`Delete this ${edge.kind} edge?`)) return;
  try {
    await apiSend("DELETE", `/edges/${encodeURIComponent(edge.uuid)}`);
    openNode(currentUuid);
  } catch (error) {
    nodeStatus.textContent = error.message;
  }
}

// ── Graph view (inline SVG node-link diagram) ────────────────────────────────

const GROUP_COLORS = {
  entity: "#dbe7f3",
  literature: "#dfe6d4",
  note: "#f6e6c4",
};

/** Light fill colour for a node-kind's group (black text reads on all). */
function groupColor(kind) {
  return GROUP_COLORS[kindToGroup.get(kind)] || "#e8e8e6";
}

graphButton.addEventListener("click", runGraph);

/** Expand the open node and render the subgraph as an inline SVG diagram. */
async function runGraph() {
  if (!currentUuid) return;
  const depth = graphDepth.value || "1";
  graphStatus.textContent = "Expanding…";
  try {
    const result = await apiGet(
      `/expand?seed=${encodeURIComponent(currentUuid)}&depth=${encodeURIComponent(depth)}`,
    );
    renderGraph(result);
  } catch (error) {
    graphStatus.textContent = `Expand failed: ${error.message}`;
    graphDetail.hidden = true;
  }
}

/** Render a Subgraph as a circular-layout SVG node-link diagram. */
function renderGraph(subgraph) {
  graphStatus.textContent =
    `depth ${subgraph.depth}: ${subgraph.nodes.length} node(s), ` +
    `${subgraph.edges.length} edge(s)`;

  const width = 640;
  const height = 420;
  const cx = width / 2;
  const cy = height / 2;
  const radius = 160;
  const r = 22;

  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, class: "graph-svg", role: "img" });
  svg.setAttribute("aria-label", "subgraph diagram");

  const defs = svgEl("defs");
  const marker = svgEl("marker", {
    id: "arrow",
    viewBox: "0 0 10 10",
    refX: 9,
    refY: 5,
    markerWidth: 7,
    markerHeight: 7,
    orient: "auto-start-reverse",
  });
  marker.appendChild(svgEl("path", { d: "M0,0 L10,5 L0,10 z", fill: "#7a7a7a" }));
  defs.appendChild(marker);
  svg.appendChild(defs);

  const count = subgraph.nodes.length;
  const positions = new Map();
  subgraph.nodes.forEach((node, index) => {
    let x = cx;
    let y = cy;
    if (count > 1) {
      const angle = (2 * Math.PI * index) / count - Math.PI / 2;
      x = cx + radius * Math.cos(angle);
      y = cy + radius * Math.sin(angle);
    }
    positions.set(String(node.uuid), { x, y, node });
  });

  // Edges first, so node circles sit on top of the lines.
  const edgeLayer = svgEl("g", { class: "edge-layer" });
  for (const edge of subgraph.edges) {
    const from = positions.get(String(edge.from_uuid));
    const to = positions.get(String(edge.to_uuid));
    if (!from || !to || from === to) continue;
    const dx = to.x - from.x;
    const dy = to.y - from.y;
    const len = Math.hypot(dx, dy) || 1;
    const ux = dx / len;
    const uy = dy / len;
    const sx = from.x + ux * r;
    const sy = from.y + uy * r;
    const ex = to.x - ux * r;
    const ey = to.y - uy * r;
    edgeLayer.appendChild(
      svgEl("line", { x1: sx, y1: sy, x2: ex, y2: ey, class: "graph-edge", "marker-end": "url(#arrow)" }),
    );
    const label = svgEl("text", {
      x: (sx + ex) / 2,
      y: (sy + ey) / 2 - 2,
      class: "graph-edge-label",
      "text-anchor": "middle",
    });
    label.textContent = edge.kind;
    edgeLayer.appendChild(label);
  }
  svg.appendChild(edgeLayer);

  const nodeLayer = svgEl("g", { class: "node-layer" });
  for (const { x, y, node } of positions.values()) {
    const group = svgEl("g", { class: "graph-node", tabindex: 0 });
    group.appendChild(
      svgEl("circle", { cx: x, cy: y, r, fill: groupColor(node.kind), stroke: "#5a5a5a", "stroke-width": 1 }),
    );
    const title = svgEl("title");
    title.textContent = `${node.kind}: ${nodeText(node.data)}`;
    group.appendChild(title);
    const label = svgEl("text", { x, y: y + 4, class: "graph-node-label", "text-anchor": "middle" });
    label.textContent = truncate(nodeText(node.data), 18);
    group.appendChild(label);
    group.addEventListener("click", () => openNode(node.uuid));
    group.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openNode(node.uuid);
      }
    });
    nodeLayer.appendChild(group);
  }
  svg.appendChild(nodeLayer);

  graphDetail.hidden = false;
  setChildren(graphDetail, [svg]);
}

// ── Bootstrap ────────────────────────────────────────────────────────────────

loadSchema();
