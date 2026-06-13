"""PostgreSQL access — connection management and schema initialisation.

Connections use ``dict_row`` so query results map cleanly onto the pydantic
models in :mod:`nodum.models`. The schema lives in ``schema.sql`` (package
data) and is applied idempotently by :func:`init_schema`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources

import psycopg
from psycopg.rows import dict_row

from nodum.settings import load_settings


@contextmanager
def connect(database_url: str | None = None) -> Iterator[psycopg.Connection]:
    """Yield a PostgreSQL connection with ``dict_row`` rows.

    Args:
        database_url: Override the configured connection string. When omitted,
            it is read from the environment via :func:`load_settings`.
    """
    url = database_url or load_settings().database_url
    with psycopg.connect(url, row_factory=dict_row) as conn:
        yield conn


def _schema_sql() -> str:
    """Return the DDL shipped as package data."""
    return resources.files("nodum").joinpath("schema.sql").read_text(encoding="utf-8")


def init_schema(conn: psycopg.Connection) -> None:
    """Create the ``nodes`` and ``edges`` tables and indexes if absent."""
    with conn.cursor() as cur:
        cur.execute(_schema_sql())
    conn.commit()
