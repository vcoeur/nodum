"""nodum — a DB-native knowledge graph.

One SQLite file holds a typed graph of Markdown-content nodes and typed edges;
every mutation flows through the deterministic service layer and is recorded in
an append-only event log with node versions, undo, and wikilink materialization.
"""
