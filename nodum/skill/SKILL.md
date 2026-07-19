---
name: nodum
description: Drive a nodum typed-graph knowledge system via the `nodum` CLI — search, read nodes with their neighbourhood, expand subgraphs, and create/edit typed nodes and edges. Use whenever the user wants to query or write to a nodum graph. This is the CLI contract; layer your own graph conventions (kinds, tags, workflows) in a separate skill on top.
argument-hint: "<natural-language request, or a nodum subcommand>"
allowed-tools: Bash(nodum:*), Bash(uv run nodum:*), Bash(jq:*), Bash(python3:*), Bash(command -v nodum:*)
---

Thin, **convention-free** wrapper around the [`nodum`](https://github.com/vcoeur/nodum) CLI. It encodes how to drive the tool *correctly and safely* — JSON parsing, error handling, batch writes, shell-quoting. It deliberately encodes **no graph conventions** (which kinds to use, how to tag, when to link). Those belong in a separate, user-specific skill that references this one. Discover what a given graph enforces with `nodum schema`.

## Use at your own risk

`nodum` is MIT software, **as-is, no warranty**. This skill runs write commands that mutate the graph `NODUM_DATABASE_URL` points at. Deletes are **hard deletes** — `rm-node` cascades to every incident edge and there is no trash. **Never run `nodum rm-node` / `rm-edge` / `node-kind rm` / `edge-kind rm` without explicit user confirmation.**

## 1 — Prerequisites

```bash
command -v nodum || pipx install nodum   # or: uv tool install nodum
```

The CLI talks straight to PostgreSQL (`NODUM_DATABASE_URL`, default `postgresql://nodum:nodum@localhost:5436/nodum`). If the database is unreachable the command fails — say so and stop. **Never hand-write SQL against the graph** — the service layer validates kinds, signatures, and payloads; raw SQL bypasses all of it.

## 2 — The JSON contract

Every command prints exactly **one JSON object to stdout** on success; human messages and errors go to **stderr** with exit code 1. So `nodum … > out.json` always captures clean JSON, and a non-zero exit means read stderr, not stdout. Parse with `jq` or `python3 -c "import sys,json; ..."`.

## 3 — Discover the contract: `nodum schema`

Before writing into an unfamiliar graph, dump the live schema once:

```bash
nodum schema
```

It returns every node kind (with its field schema and what its `content` means) and every edge kind (with its `from → to` signature), each annotated with a `usage` count. Kinds are **data** — the catalog evolves at runtime, so never hardcode kind names; read them off `schema`. Validation is write-time: an unknown kind, a missing required field, a wrong field type, or an edge whose endpoints fall outside the signature is rejected with a stderr message and exit 1.

## 4 — Data model cheat sheet

- A **node** is `{uuid, kind, title, content, data, created_at, updated_at}`. `content` is the universal plain-text body (full-text indexed); `data` is the kind's typed payload. `title` is derived from the first non-blank line of `content` — write a strong first line.
- An **edge** is `{uuid, kind, from_uuid, to_uuid, data, …}` — directed, signature-checked.
- **Tags** are a convention: any node may carry `"tags": ["…"]` in its `data`. Filter with `search --tag` (repeatable, AND semantics).

## 5 — Command cheat sheet

### Read path
| Command | Purpose |
|---|---|
| `nodum search "<q>" [--kind K] [--tag T …] [--limit N]` | Ranked full-text search. Each hit's `content` is a 200-char **snippet** by default (`content_truncated` + `content_total_chars` mark a cut). `--fields full` restores full content; `--fields minimal` returns `{uuid, kind, title, score}` only; `--max-body-chars N` sets the cut explicitly. |
| `nodum get <uuid> [<uuid2> …]` | Node(s) + incident edges. One UUID → `{node, edges}`; several → `{targets, nodes, failed}` (a miss never aborts the rest). `--edge-kind K` (repeatable) and `--direction in\|out\|both` filter the incident edges; `--fields minimal` trims each node to `{uuid, kind, title}`; `--max-body-chars N` truncates content. |
| `nodum expand <uuid> [--depth N] [--edge-kind K …]` | Seed → connected subgraph (`{seed, depth, nodes, edges}`), the context payload. |
| `nodum schema` | The live schema — always the first call in an unfamiliar graph. |

### Write path
| Command | Purpose |
|---|---|
| `nodum add KIND "CONTENT" [--set k=v …]` | New typed node. |
| `nodum add --batch FILE\|-` | Bulk create from a JSON array of `{kind, content, data?}` — see §6. `--dry-run` validates without writing. |
| `nodum link FROM TO EDGE_KIND [--set k=v …]` | New typed, directed edge (signature-checked). |
| `nodum edit-node UUID [--content …] [--set k=v …]` | Merge into a node (re-validated). |
| `nodum edit-node --batch FILE\|-` | Bulk edit from a JSON array of `{uuid, content?, data?}`. `--dry-run` supported. |
| `nodum rm-node UUID` / `rm-edge UUID` | **Hard** delete (nodes cascade to edges). Confirm with the user first. |
| `nodum node-kind add/edit/rm …` / `edge-kind add/edit/rm …` | Evolve the schema itself (runtime kind CRUD). |

`--set key=value` is repeatable; each value is parsed as JSON, falling back to a raw string (`--set born=1815` → int, `--set 'tags=["a","b"]'` → list, `--set venue=Nature` → string).

## 6 — Batch writes: one call, not N

For **3+ creates or edits**, do not chain individual `add` / `edit-node` calls — that's N connections and N rounds of shell escaping. Pipe a JSON array instead:

```bash
python3 - <<'PY' | nodum add --batch -
import json
print(json.dumps([
  {"kind": "Note", "content": "Spaced repetition beats cramming.", "data": {"role": "claim", "tags": ["learning"]}},
  {"kind": "Note", "content": "Retrieval practice is the active ingredient.", "data": {"role": "hypothesis"}},
]))
PY
```

The result is `{operation, count, succeeded, failed, dry_run, results: [{index, ok, uuid|error}]}` — one bad item does not abort the rest. Exit code is 1 when any item failed, with the full JSON summary still on stdout. Add `--dry-run` to validate every item without writing. `edit-node --batch` mirrors this for edits (items are `{uuid, content?, data?}`, same merge semantics as the single form).

## 7 — Reading large payloads on a budget

- Prefer `search` (snippets) over `get` for discovery; `get` only the UUIDs you actually need — `get` accepts several UUIDs in one call.
- Use `--fields minimal` to list candidates cheaply, then fetch full bodies for the shortlist.
- Use `--max-body-chars N` when you only need the lead of a long body; the `content_truncated` flag tells you more exists.

## 8 — Avoid shell-quoting hazards (critical for writes)

Node content routinely contains `$`, backticks, quotes, and newlines. Two rules:

1. **Never inline long or special-character content as a positional arg.** Build the payload in Python and pipe it via `add --batch -` (works for a single item too — a one-element array).
2. **Never use `sed`/regex to rewrite stored content.** Fetch with `get`, transform in Python, write back with `edit-node --content` or `--batch`.

---

## Installation

```bash
nodum skill install --user        # -> ~/.config/agents/skills/nodum/SKILL.md (default)
nodum skill install --project     # -> <cwd>/.agents/skills/nodum/SKILL.md
nodum skill install --dest DIR    # explicit directory
nodum skill status                # where it's installed and whether it matches the bundled copy
```

To add graph conventions (which kinds to use, tagging schemes, ingest flows), fork into a **separate** skill that references this one for mechanics — keep this file convention-free so it updates cleanly with the tool.

$ARGUMENTS
