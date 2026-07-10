"""DAO for ``cooldown_suppressions`` — telemetry of intents dropped by the
kind-level hard cooldown (#533).

When the cooldown gate (:mod:`persome.intent.cooldown`) drops an intent at
the unified sink, the intent never reaches the ``intents`` table — so without a
side-channel the suppression is invisible to ``stats`` / eval / the recalibration
work (#534). The asymmetric-cost constitution lets us闸掉 the **presentation** of
a cooled-down kind, but NEVER its **observability**: "拒绝是金矿" — these are
exactly the negative datapoints the recalibration (#534) needs. This table is the
金矿's ledger.

Pure audit trail — like ``recognition_ticks``. It is written
best-effort (a failure never blocks the suppression itself) and read by
``/intents/stats`` so the real-world suppression rate is measurable.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from ..logger import get

logger = get("persome.store.cooldown_suppressions")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cooldown_suppressions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,                 -- ISO8601 of the suppressed intent's recognition (intent.ts)
    kind TEXT NOT NULL,
    scope TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.0,
    cooldown_until TEXT,              -- ISO8601 when the cooldown that dropped it expires (NULL if unknown)
    created_at TEXT NOT NULL          -- ISO8601 when the suppression was recorded
);
CREATE INDEX IF NOT EXISTS idx_cooldown_suppressions_ts ON cooldown_suppressions(ts DESC);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def record(
    conn: sqlite3.Connection,
    *,
    ts: str,
    kind: str,
    scope: str,
    confidence: float,
    cooldown_until: str | None = None,
) -> int:
    """Insert one suppression row. Returns the row id (0 on a swallowed failure).

    Best-effort: any DB error is logged and swallowed — recording the金矿 must
    never break the write path it observes (the suppression already happened).
    """
    try:
        ensure_schema(conn)
        cur = conn.execute(
            """
            INSERT INTO cooldown_suppressions
                (ts, kind, scope, confidence, cooldown_until, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                kind,
                scope,
                float(confidence),
                cooldown_until,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    except Exception as exc:  # noqa: BLE001 — telemetry must never break the sink
        logger.warning("cooldown suppression telemetry write failed (ignored): %s", exc)
        return 0


def stats(conn: sqlite3.Connection, *, since: str = "", until: str = "￿") -> dict:
    """Suppression telemetry over ``[since, until)`` by ``ts``.

    Returns the total number of suppressed intents in the window and a per-kind
    breakdown — the denominator the ``intents`` table cannot provide (a dropped
    intent leaves no row). Feeds ``/intents/stats`` so the cooldown's real-world
    bite is measurable and the #534 recalibration has data to work from.
    """
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT kind FROM cooldown_suppressions WHERE ts >= ? AND ts < ?",
        (since, until),
    ).fetchall()
    by_kind: dict[str, int] = {}
    for r in rows:
        k = str(r["kind"])
        by_kind[k] = by_kind.get(k, 0) + 1
    return {"total": len(rows), "by_kind": by_kind}


def prune(conn: sqlite3.Connection, *, keep: int = 50000) -> int:
    """Keep only the most recent ``keep`` rows (bounded telemetry)."""
    ensure_schema(conn)
    cur = conn.execute(
        "DELETE FROM cooldown_suppressions WHERE id NOT IN "
        "(SELECT id FROM cooldown_suppressions ORDER BY id DESC LIMIT ?)",
        (keep,),
    )
    conn.commit()
    return cur.rowcount
