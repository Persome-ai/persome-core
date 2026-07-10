"""DAO for ``parser_ticks`` — per-window telemetry of the per-app message parsers.

Every time the timeline aggregator builds a block, it inspects the window's
captures for a registered per-app parser (``FeishuParser`` and friends) and
records exactly one row here describing the outcome:

- ``hit``      — a parseable capture's app had a parser and ``render()`` was non-empty.
- ``miss``     — the app had a parser but it declined (``parse`` → ``None``),
                 rendered empty, or raised.
- ``fallback`` — no capture in the window belonged to an app with a registered
                 parser (modeling falls back to the raw ``focus_excerpt``).

This is the **general observability layer**: it answers "are the parsers
actually firing, and for which apps?" bucketed by ``bundle_id``. A drift in the
hit rate for a given bundle (e.g. 飞书 ships a UI revision that breaks the
semantic classes ``FeishuParser`` keys on) shows up here as ``hit`` decaying
into ``miss`` early, before modeled memory loses that signal.

Canonical parser output still lives on the timeline block's
``focus_structured`` column; this table is telemetry only.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from ..logger import get

logger = get("persome.store.parser_ticks")

# Allowed outcomes. Kept as a module constant so callers and tests share one
# source of truth and ``stats`` can zero-fill every bucket deterministically.
OUTCOMES: tuple[str, ...] = ("hit", "miss", "fallback")

SCHEMA = """
CREATE TABLE IF NOT EXISTS parser_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,              -- ISO8601 window time (block start)
    bundle_id TEXT NOT NULL,       -- app bundle the outcome is attributed to
    outcome TEXT NOT NULL,         -- 'hit' | 'miss' | 'fallback'
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_parser_ticks_ts ON parser_ticks(ts DESC);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def record_tick(
    conn: sqlite3.Connection,
    *,
    ts: str,
    bundle_id: str,
    outcome: str,
) -> int:
    """Insert one parser-tick telemetry row. Returns the row id.

    ``outcome`` is stored verbatim; unknown values are tolerated by the table
    but ``stats`` only zero-fills the canonical :data:`OUTCOMES` buckets, so
    callers should pass one of those.
    """
    ensure_schema(conn)
    cur = conn.execute(
        """
        INSERT INTO parser_ticks (ts, bundle_id, outcome, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            ts,
            str(bundle_id or ""),
            str(outcome or ""),
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def stats(conn: sqlite3.Connection, *, since: str = "", until: str = "￿") -> dict:
    """Parser hit/miss/fallback telemetry over ``[since, until)`` by ts.

    Returns:
        - ``total``      — number of ticks in the window.
        - ``by_outcome`` — ``{hit, miss, fallback}`` counts (always all three keys).
        - ``by_bundle``  — ``{<bundle_id>: {hit, miss, fallback}}`` per-app breakdown.
        - ``hit_rate``   — ``hit / total`` (i.e. hit ÷ every recorded tick,
                            counting fallback windows as non-hits). Rounded to 4
                            decimals; ``0.0`` when there are no ticks.
        - ``since`` / ``until`` — echoed bounds (``None`` when unbounded).

    ``hit_rate`` deliberately uses ``hit / total`` rather than
    ``hit / (hit + miss)``: a window where no parseable app was open
    (``fallback``) is still a window the parsers did NOT help, so it belongs in
    the denominator when judging overall parser coverage. Per-bundle precision
    (``hit / (hit + miss)`` for one app) can be derived from ``by_bundle``.
    """
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT bundle_id, outcome FROM parser_ticks WHERE ts >= ? AND ts < ?",
        (since, until),
    ).fetchall()
    total = len(rows)
    by_outcome: dict[str, int] = {o: 0 for o in OUTCOMES}
    by_bundle: dict[str, dict[str, int]] = {}
    for r in rows:
        outcome = str(r["outcome"] or "")
        bundle = str(r["bundle_id"] or "")
        if outcome in by_outcome:
            by_outcome[outcome] += 1
        bucket = by_bundle.setdefault(bundle, {o: 0 for o in OUTCOMES})
        if outcome in bucket:
            bucket[outcome] += 1
    return {
        "total": total,
        "by_outcome": by_outcome,
        "by_bundle": by_bundle,
        "hit_rate": round(by_outcome["hit"] / total, 4) if total else 0.0,
        "since": since or None,
        "until": until if until != "￿" else None,
    }


def prune(conn: sqlite3.Connection, *, keep: int = 50000) -> int:
    """Keep only the most recent ``keep`` rows (bounded telemetry). Returns the
    number of rows deleted."""
    ensure_schema(conn)
    cur = conn.execute(
        "DELETE FROM parser_ticks WHERE id NOT IN "
        "(SELECT id FROM parser_ticks ORDER BY id DESC LIMIT ?)",
        (keep,),
    )
    conn.commit()
    return cur.rowcount
