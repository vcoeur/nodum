"""nodum — a DB-native knowledge graph.

One SQLite file holds a typed graph of Markdown-content nodes and typed edges;
every mutation flows through the deterministic service layer and is recorded in
an append-only event log with node versions, undo, and wikilink materialization.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("nodum")
except PackageNotFoundError:  # pragma: no cover — running from a non-installed checkout
    __version__ = "0.0.0+unknown"
