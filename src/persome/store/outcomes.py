"""DAO for ``outcomes`` — the reverse-loop G4 execution-result ledger (spec
2026-06-26 §3.1.2).

The app writes one row here when a proactive **follow-up** (`FollowUpCoordinator`)
or a **supervised** run finishes: did the thing the user accepted actually get
*done*? This is the daemon's only structured view of execution success — the
forward path knows what was recognized/accepted, never whether the follow-up
landed. A deterministic per-kind success rate (``kind_success_rate``) reads it,
gated on ≥N samples so a data-starved kind never drives a decision.

**Content-free red line** (same as ``AppEventLog`` / ``ContextFeedbackLog``):
ONLY enums / booleans / counts / durations — zero screen text, artifact bodies,
or secrets. The columns below are all that may ever land here.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from ..logger import get

logger = get("persome.store.outcomes")

SCHEMA = """
CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,                         -- ISO8601 outcome time (app-stamped)
    intent_id INTEGER,                        -- the intent this execution served (NULL if none)
    kind TEXT NOT NULL,                       -- intent kind: meeting|calendar|reminder|... (enum-ish)
    status TEXT NOT NULL,                     -- followup | supervised (which executor produced it)
    success INTEGER NOT NULL DEFAULT 0,       -- 0/1 — did it accomplish the accepted thing
    executor_tier TEXT,                       -- optional capability tier label (enum)
    artifact_verified INTEGER,                -- 0/1 — the produced artifact was verified (FollowUp)
    placed INTEGER,                           -- 0/1 — pasted into a focused field (never sent)
    awaited_confirm INTEGER,                  -- 0/1 — execution paused for a user confirm
    reschedule_suggested INTEGER,             -- 0/1 — a reschedule was proposed
    elapsed_ms INTEGER,                       -- wall-clock duration, ms
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outcomes_kind ON outcomes(kind);
CREATE INDEX IF NOT EXISTS idx_outcomes_ts ON outcomes(ts DESC);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def insert_outcome(
    conn: sqlite3.Connection,
    *,
    kind: str,
    status: str,
    success: bool,
    ts: str | None = None,
    intent_id: int | None = None,
    executor_tier: str | None = None,
    artifact_verified: bool | None = None,
    placed: bool | None = None,
    awaited_confirm: bool | None = None,
    reschedule_suggested: bool | None = None,
    elapsed_ms: int | None = None,
) -> int:
    """Record one content-free execution outcome. Returns the row id."""
    ensure_schema(conn)

    def _b(v: bool | None) -> int | None:
        return None if v is None else (1 if v else 0)

    now = datetime.now().astimezone().isoformat(timespec="seconds")
    cur = conn.execute(
        """
        INSERT INTO outcomes
            (ts, intent_id, kind, status, success, executor_tier, artifact_verified,
             placed, awaited_confirm, reschedule_suggested, elapsed_ms, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ts or now,
            intent_id,
            kind,
            status,
            1 if success else 0,
            executor_tier,
            _b(artifact_verified),
            _b(placed),
            _b(awaited_confirm),
            _b(reschedule_suggested),
            elapsed_ms,
            now,
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def kind_success_rate(
    conn: sqlite3.Connection, *, since: str, min_samples: int = 5
) -> list[tuple[str, int, int, float]]:
    """``(kind, n, successes, rate)`` per kind over outcomes at/after ``since``,
    for kinds with **≥ ``min_samples``** rows only.

    The ≥N gate is the data-hunger red line: a kind below the sample floor is
    omitted entirely (UNDECIDABLE — never act on noise). Per-KIND, not
    per-(kind,capability): the finer bucket would never reach the floor and would
    let a couple of early failures damn a whole capability. Deterministic order:
    rate asc (worst first, the actionable end), then kind asc.

    Read-only safe: does NOT create the table (so it can run on a ``mode=ro``
    connection), and a missing ``outcomes`` table (no insert has happened yet)
    fails open to ``[]`` rather than raising.
    """
    try:
        rows = conn.execute(
            """
            SELECT kind, COUNT(*) AS n, SUM(success) AS s FROM outcomes
            WHERE ts >= ?
            GROUP BY kind HAVING n >= ?
            ORDER BY (CAST(SUM(success) AS REAL) / COUNT(*)) ASC, kind ASC
            """,
            (since, max(1, min_samples)),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out: list[tuple[str, int, int, float]] = []
    for r in rows:
        n = int(r[1] or 0)
        s = int(r[2] or 0)
        out.append((str(r[0]), n, s, (s / n) if n else 0.0))
    return out


def prune(conn: sqlite3.Connection, *, keep: int = 50000) -> int:
    """Cap the table to the newest ``keep`` rows (daily housekeeping). Returns rows deleted.

    ``kind_success_rate`` already filters by a ``since`` window, so rows outside
    that window are dead weight; without this the ledger grows monotonically on a
    long-lived install — the #508/#533/#622 trap of a "bounded telemetry" table
    that ships a ``prune`` no one calls. Wired into ``_prune_telemetry_tables`` so
    the daily safety-net tick keeps it bounded like its sibling per-event ledgers
    (``intent_fold_ticks`` / ``fast_path_ticks`` / …)."""
    ensure_schema(conn)
    cur = conn.execute(
        "DELETE FROM outcomes WHERE id NOT IN (SELECT id FROM outcomes ORDER BY id DESC LIMIT ?)",
        (keep,),
    )
    conn.commit()
    return cur.rowcount or 0
