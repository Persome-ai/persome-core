"""DAO for ``intent_fold_ticks`` — reverse-loop G5.1 (spec 2026-06-26 §3.3).

Every time the sink FOLDS an incoming intent onto an existing row (exact
``dedup_key`` hit, cross-form, or the semantic/content fold) instead of inserting
a new one, it records ONE content-free row here: which ``scope`` + ``kind`` got
re-recognized, and onto which existing row id. The fold itself is silent today
(``persist_intent_result`` returns ``updated``/``skipped`` and moves on), so
"the user keeps re-committing the SAME thing every session" leaves no measurable
trace — and that frequency is exactly the real signal for tuning the content-fold
threshold (too tight → the same to-do logged 6×; too loose → distinct things
collapsed). This table is that denominator.

**Content-free** (same red line as ``recognition_ticks`` / ``outcomes``): only
``scope`` / ``kind`` / a row id / a timestamp — zero screen text or payload body.
Telemetry only; the canonical intent still lives in ``intents``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from ..logger import get

logger = get("persome.store.intent_fold_ticks")

SCHEMA = """
CREATE TABLE IF NOT EXISTS intent_fold_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,                 -- ISO8601 fold time
    scope TEXT NOT NULL,              -- the folding intent's scope (session-* / fast-K1 / …)
    kind TEXT NOT NULL,               -- the intent kind that folded (meeting/reminder/…)
    target_id INTEGER,                -- the existing intents row it folded ONTO (NULL if a bare dedup hit)
    outcome TEXT NOT NULL,            -- fold | updated (material re-recognition) | skipped (pure dup)
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intent_fold_ticks_ts ON intent_fold_ticks(ts DESC);
CREATE INDEX IF NOT EXISTS idx_intent_fold_ticks_kind ON intent_fold_ticks(kind);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def record_fold(
    conn: sqlite3.Connection,
    *,
    scope: str,
    kind: str,
    target_id: int | None = None,
    outcome: str = "fold",
    ts: str | None = None,
) -> None:
    """Record one fold event. Best-effort + fail-open: any error is swallowed so a
    telemetry write can NEVER perturb the sink's dedup/fold decision (the canonical
    write already committed)."""
    try:
        ensure_schema(conn)
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO intent_fold_ticks (ts, scope, kind, target_id, outcome, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts or now, scope, kind, target_id, outcome, now),
        )
        conn.commit()
    except Exception:  # noqa: BLE001 — telemetry must never break the hot write path
        logger.debug("intent_fold_ticks record failed", exc_info=True)


def fold_heat(
    conn: sqlite3.Connection, *, since: str, limit: int = 20
) -> list[tuple[str, int, int]]:
    """``(kind, folds, distinct_targets)`` per kind over folds at/after ``since``,
    busiest first. ``folds / distinct_targets`` ≈ how many times the SAME fact is
    re-recognized — the content-fold tuning signal. Read-only safe: a missing
    table fails open to ``[]``."""
    try:
        rows = conn.execute(
            """
            SELECT kind, COUNT(*) AS folds, COUNT(DISTINCT target_id) AS targets
            FROM intent_fold_ticks
            WHERE ts >= ?
            GROUP BY kind
            ORDER BY folds DESC, kind ASC
            LIMIT ?
            """,
            (since, max(1, limit)),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(str(r[0]), int(r[1] or 0), int(r[2] or 0)) for r in rows]


def prune(conn: sqlite3.Connection, *, keep: int = 50000) -> int:
    """Cap the table to the newest ``keep`` rows (daily housekeeping). Returns rows deleted."""
    ensure_schema(conn)
    cur = conn.execute(
        "DELETE FROM intent_fold_ticks WHERE id NOT IN "
        "(SELECT id FROM intent_fold_ticks ORDER BY id DESC LIMIT ?)",
        (keep,),
    )
    conn.commit()
    return cur.rowcount or 0
