"""Connection-level behaviour of :mod:`nodum.db` — the busy timeout and write contention."""

from __future__ import annotations

import threading
import time

from nodum import db


def test_connect_sets_the_busy_timeout(fresh_db):
    """M16: a writer must out-wait a big registration's hold on the write lock.

    ``register_asset`` streams a whole asset in one transaction, and the
    measured 200 MB → 1.22 s copy rate extrapolates to ≈ 6 s at the 1 GB
    ceiling — past the 5 s default Python applies, which would fail every
    concurrent writer that collided. The connection must therefore carry
    :data:`nodum.db.BUSY_TIMEOUT_MS`, not the default.
    """
    conn = db.connect(fresh_db)
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db.BUSY_TIMEOUT_MS
    finally:
        conn.close()


def test_a_contending_write_waits_out_a_long_write_lock_hold(fresh_db):
    """M16: the raised timeout is real, not a documented value.

    A write lock held ~6.5 s — past the old 5 s default, inside the 15 s one —
    must be waited out by a concurrent write, which under the default timeout
    would have died at 5 s with "database is locked". The runtime is ~7 s by
    design: the whole point is that the wait is real.
    """
    blocker = db.connect(fresh_db)
    blocker.execute("BEGIN EXCLUSIVE")
    outcome: dict[str, object] = {}

    def contend() -> None:
        conn = db.connect(fresh_db)
        try:
            outcome["started"] = time.monotonic()
            conn.execute("CREATE TABLE busy_probe (x INTEGER)")
            conn.commit()
            outcome["succeeded"] = True
        except Exception as exc:  # surfaced by the asserts below
            outcome["error"] = exc
        finally:
            conn.close()

    thread = threading.Thread(target=contend)
    thread.start()
    time.sleep(6.5)
    blocker.rollback()
    blocker.close()
    thread.join(timeout=10)
    assert not thread.is_alive(), "the contending write never finished"
    assert "succeeded" in outcome, f"the contender failed: {outcome.get('error')!r}"
    waited = time.monotonic() - outcome["started"]
    assert waited >= 6.0, f"the write should have waited out the ~6.5 s hold, got {waited:.2f}s"
