"""DAO for windowed structured memory deltas and their apply audit.

One LLM reading of each newly flushed window emits a structured
``memory_delta {owner_alias_candidates, entities, assertions, relations, events}``.
The post-gate
payload is persisted here before deterministic application mints Points and
Lines. ``apply_status`` makes interrupted application retryable without
spending another LLM call or reinforcing an edge twice.

Rows are append-only; active-session flushes and terminal finalization create
one row per non-overlapping window and resume application from that row. The
payload column stores
the POST-GATE delta (after the deterministic quote/roster/predicate/confidence
gates in ``writer/memory_delta.py``), plus a ``dropped`` audit count so gate
strictness stays observable.

``memory_delta_evidence_claims`` is the durable consumption receipt for the evidence
actually sent to the model. Complete wall-clock windows use their timeline
block ID plus the bounded source-capture IDs recorded in the block's durable
source manifest.
A boundary window that must be clipped to an exact session cutoff uses only its
source capture IDs, so adjacent off-minute windows consume disjoint evidence
without sending text beyond either cutoff. The receipts and delta row commit
atomically: extraction or persistence failure leaves evidence retryable, while
apply failure keeps the receipts attached to the payload.

``memory_delta_apply_receipts`` makes the only additive apply effects
crash-idempotent. Before an attention-floor or co-occurrence edge mutates, its
``(delta_id, effect_key)`` receipt durably freezes the next absolute
``observations`` target within one edge validity generation. Retrying the same
delta uses ``MAX(target)`` rather than another increment, while a different
delta reserves the next target. The first receipt also permanently binds that
delta/effect to its original generation; a retry after close/reopen is a safe
no-op instead of rebinding old evidence to the new interval.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import cast

from ..capture.timestamps import parse_capture_path_timestamp, parse_capture_timestamp
from ..logger import get

logger = get("persome.store.memory_deltas")

STATUS_SHADOW = "shadow"
_REQUIRED_DELTA_COLUMNS = frozenset(
    {
        "id",
        "session_id",
        "created_at",
        "model",
        "status",
        "payload",
        "dropped",
        "apply_status",
        "window_start",
        "window_end",
        "is_final",
    }
)
_REQUIRED_TABLES = frozenset(
    {
        "memory_deltas",
        "memory_delta_evidence_claims",
        "memory_delta_apply_receipts",
    }
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_deltas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    created_at TEXT NOT NULL,            -- ISO8601 consolidation time
    model TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'shadow',
    payload TEXT NOT NULL DEFAULT '{}',  -- post-gate delta JSON
    dropped INTEGER NOT NULL DEFAULT 0,  -- items removed by the deterministic gates
    apply_status TEXT NOT NULL DEFAULT 'unknown',
    window_start TEXT NOT NULL DEFAULT '',
    window_end TEXT NOT NULL DEFAULT '',
    is_final INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_memory_deltas_session ON memory_deltas(session_id);
CREATE INDEX IF NOT EXISTS idx_memory_deltas_created ON memory_deltas(created_at DESC);

CREATE TABLE IF NOT EXISTS memory_delta_evidence_claims (
    evidence_id TEXT PRIMARY KEY,
    delta_id INTEGER NOT NULL REFERENCES memory_deltas(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    UNIQUE(delta_id, ordinal)
);

CREATE TABLE IF NOT EXISTS memory_delta_apply_receipts (
    delta_id INTEGER NOT NULL REFERENCES memory_deltas(id) ON DELETE CASCADE,
    effect_key TEXT NOT NULL,
    target_observations INTEGER NOT NULL CHECK(target_observations >= 1),
    created_at TEXT NOT NULL,
    PRIMARY KEY(delta_id, effect_key)
);
CREATE INDEX IF NOT EXISTS idx_memory_delta_apply_effect
    ON memory_delta_apply_receipts(effect_key, target_observations DESC);
"""


def _schema_ready(conn: sqlite3.Connection) -> bool:
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name IN"
            " ('memory_deltas', 'memory_delta_evidence_claims',"
            "  'memory_delta_apply_receipts')"
        ).fetchall()
    }
    if tables != _REQUIRED_TABLES:
        return False
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memory_deltas)")}
    return columns >= _REQUIRED_DELTA_COLUMNS


def ensure_schema(conn: sqlite3.Connection) -> None:
    from . import fts

    if fts.is_client_process():
        return
    if conn.in_transaction:
        # sqlite3.executescript() implicitly commits. Never run it inside a
        # caller-owned transaction: either the daemon initialized this schema
        # already, or the caller must do so before BEGIN.
        if _schema_ready(conn):
            return
        raise RuntimeError(
            "memory_delta schema is not initialized; call ensure_schema before BEGIN"
        )
    conn.executescript(SCHEMA)
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memory_deltas)")}
    if "apply_status" not in columns:
        # Old rows may or may not have been applied. Treat them as processed;
        # only rows written by the new code carry a retryable failed state.
        conn.execute(
            "ALTER TABLE memory_deltas ADD COLUMN apply_status TEXT NOT NULL DEFAULT 'unknown'"
        )
    if "window_start" not in columns:
        conn.execute("ALTER TABLE memory_deltas ADD COLUMN window_start TEXT NOT NULL DEFAULT ''")
    if "window_end" not in columns:
        conn.execute("ALTER TABLE memory_deltas ADD COLUMN window_end TEXT NOT NULL DEFAULT ''")
    if "is_final" not in columns:
        conn.execute("ALTER TABLE memory_deltas ADD COLUMN is_final INTEGER NOT NULL DEFAULT 1")


def insert(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    payload: dict,
    model: str = "",
    dropped: int = 0,
    status: str = STATUS_SHADOW,
    apply_status: str = "not_requested",
    created_at: datetime | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    is_final: bool = True,
    evidence_ids: Sequence[str] = (),
) -> int:
    """Insert one delta and atomically claim its consumed evidence IDs.

    ``evidence_id`` is either a timeline block ID or a cutoff-safe source-capture
    ID. It is globally unique. A collision
    rolls back the new delta row as well as all of its claims, which is the DB
    backstop for callers that bypass the process-wide session-model lock.
    """
    ensure_schema(conn)
    ordered_evidence_ids = tuple(evidence_ids)
    if any(
        not isinstance(evidence_id, str) or not evidence_id.strip()
        for evidence_id in ordered_evidence_ids
    ):
        raise ValueError("memory_delta evidence_ids must be non-empty strings")
    if len(ordered_evidence_ids) != len(set(ordered_evidence_ids)):
        raise ValueError("memory_delta evidence_ids must be unique")

    if not ordered_evidence_ids:
        return _insert_row(
            conn,
            session_id=session_id,
            payload=payload,
            model=model,
            dropped=dropped,
            status=status,
            apply_status=apply_status,
            created_at=created_at,
            window_start=window_start,
            window_end=window_end,
            is_final=is_final,
        )

    nested = conn.in_transaction
    if nested:
        conn.execute("SAVEPOINT memory_delta_with_evidence")
    else:
        conn.execute("BEGIN IMMEDIATE")
    try:
        delta_id = _insert_row(
            conn,
            session_id=session_id,
            payload=payload,
            model=model,
            dropped=dropped,
            status=status,
            apply_status=apply_status,
            created_at=created_at,
            window_start=window_start,
            window_end=window_end,
            is_final=is_final,
        )
        conn.executemany(
            "INSERT INTO memory_delta_evidence_claims"
            " (evidence_id, delta_id, ordinal) VALUES (?, ?, ?)",
            (
                (evidence_id, delta_id, ordinal)
                for ordinal, evidence_id in enumerate(ordered_evidence_ids)
            ),
        )
        if nested:
            conn.execute("RELEASE SAVEPOINT memory_delta_with_evidence")
        else:
            conn.commit()
        return delta_id
    except Exception:
        if nested:
            conn.execute("ROLLBACK TO SAVEPOINT memory_delta_with_evidence")
            conn.execute("RELEASE SAVEPOINT memory_delta_with_evidence")
        else:
            conn.rollback()
        raise


def _insert_row(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    payload: dict,
    model: str,
    dropped: int,
    status: str,
    apply_status: str,
    created_at: datetime | None,
    window_start: datetime | None,
    window_end: datetime | None,
    is_final: bool,
) -> int:
    ts = (created_at or datetime.now().astimezone()).isoformat()
    cur = conn.execute(
        "INSERT INTO memory_deltas"
        " (session_id, created_at, model, status, payload, dropped, apply_status,"
        " window_start, window_end, is_final)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            ts,
            model,
            status,
            json.dumps(payload, ensure_ascii=False),
            dropped,
            apply_status,
            window_start.isoformat() if window_start else "",
            window_end.isoformat() if window_end else "",
            1 if is_final else 0,
        ),
    )
    return int(cur.lastrowid or 0)


def evidence_ids_for_delta(conn: sqlite3.Connection, delta_id: int) -> list[str]:
    """Return one delta's consumed evidence IDs in prompt order."""
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT evidence_id FROM memory_delta_evidence_claims"
        " WHERE delta_id=? ORDER BY ordinal ASC",
        (delta_id,),
    ).fetchall()
    return [str(row[0]) for row in rows]


def claimed_evidence_ids(conn: sqlite3.Connection, evidence_ids: Sequence[str]) -> set[str]:
    """Return the requested evidence IDs that already have a durable receipt."""
    ensure_schema(conn)
    wanted = tuple(dict.fromkeys(str(evidence_id) for evidence_id in evidence_ids if evidence_id))
    claimed: set[str] = set()
    # Stay below SQLite's host-parameter limit even for 200 windows with many
    # source captures each.
    for offset in range(0, len(wanted), 400):
        chunk = wanted[offset : offset + 400]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT evidence_id FROM memory_delta_evidence_claims"
            f" WHERE evidence_id IN ({placeholders})",  # noqa: S608
            chunk,
        ).fetchall()
        claimed.update(str(row[0]) for row in rows)
    return claimed


def claimed_capture_evidence_times(
    conn: sqlite3.Connection,
    *,
    start: datetime,
    end: datetime,
) -> dict[str, datetime]:
    """Return capture receipts whose durable ID timestamp falls in ``[start,end)``.

    Legacy timeline blocks predate ``timeline_block_sources``. Parsing the
    timestamp-bearing capture ID lets cutoff logic detect an older partial
    claim even after its raw JSON was retained away.
    """
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT evidence_id FROM memory_delta_evidence_claims WHERE evidence_id LIKE 'capture:%'"
    ).fetchall()
    matches: dict[str, datetime] = {}
    for row in rows:
        evidence_id = str(row[0] or "")
        stem = evidence_id.removeprefix("capture:")
        timestamp = parse_capture_path_timestamp(Path(f"{stem}.json"))
        if timestamp is not None and start <= timestamp < end:
            matches[evidence_id] = timestamp
    return matches


def unparseable_capture_claim_windows(
    conn: sqlite3.Connection,
    *,
    start: datetime,
    end: datetime,
) -> tuple[tuple[datetime, datetime] | None, ...]:
    """Return overlapping delta windows for capture claims with opaque IDs.

    Old/manual receipts may not carry a reversible timestamp in the capture ID.
    Their owning delta window is the only durable location signal. ``None``
    means even that window is absent or malformed, so callers must treat the
    claim as potentially overlapping every legacy block.
    """
    ensure_schema(conn)
    start_utc = parse_capture_timestamp(start.isoformat())
    end_utc = parse_capture_timestamp(end.isoformat())
    if start_utc is None or end_utc is None:
        return (None,)
    rows = conn.execute(
        "SELECT c.evidence_id, d.window_start, d.window_end"
        " FROM memory_delta_evidence_claims c"
        " JOIN memory_deltas d ON d.id=c.delta_id"
        " WHERE c.evidence_id LIKE 'capture:%'"
    ).fetchall()
    windows: list[tuple[datetime, datetime] | None] = []
    for row in rows:
        evidence_id = str(row[0] or "")
        stem = evidence_id.removeprefix("capture:")
        if parse_capture_path_timestamp(Path(f"{stem}.json")) is not None:
            continue
        window_start = parse_capture_timestamp(str(row[1] or ""))
        window_end = parse_capture_timestamp(str(row[2] or ""))
        if window_start is None or window_end is None:
            windows.append(None)
        elif window_end > start_utc and window_start < end_utc:
            windows.append((window_start, window_end))
    return tuple(windows)


def recent(conn: sqlite3.Connection, *, limit: int = 20) -> list[sqlite3.Row]:
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    return list(
        conn.execute(
            "SELECT * FROM memory_deltas ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        )
    )


def latest_for_session(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM memory_deltas WHERE session_id = ?"
        " ORDER BY created_at DESC, id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return cast(sqlite3.Row | None, row)


def latest_for_window(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    window_start: datetime,
    window_end: datetime,
) -> sqlite3.Row | None:
    """Return the newest attempt for one exact incremental modeling window."""
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM memory_deltas WHERE session_id=? AND window_start=? AND window_end=?"
        " ORDER BY id DESC LIMIT 1",
        (session_id, window_start.isoformat(), window_end.isoformat()),
    ).fetchone()
    return cast(sqlite3.Row | None, row)


def next_for_session_start(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    window_start: datetime,
    through: datetime,
) -> sqlite3.Row | None:
    """Return the earliest persisted window beginning at one watermark."""
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM memory_deltas m WHERE session_id=? AND window_start=?"
        " AND window_end>window_start AND window_end<=?"
        " AND id=(SELECT MAX(id) FROM memory_deltas WHERE session_id=m.session_id"
        " AND window_start=m.window_start AND window_end=m.window_end)"
        " ORDER BY window_end ASC LIMIT 1",
        (session_id, window_start.isoformat(), through.isoformat()),
    ).fetchone()
    return cast(sqlite3.Row | None, row)


def set_apply_status(conn: sqlite3.Connection, delta_id: int, status: str) -> None:
    ensure_schema(conn)
    conn.execute("UPDATE memory_deltas SET apply_status=? WHERE id=?", (status, delta_id))


def reserve_additive_target(
    conn: sqlite3.Connection,
    *,
    delta_id: int,
    effect_key: str,
    src_identity: str,
    dst_identity: str,
    predicate: str,
) -> tuple[int, str | None, bool]:
    """Durably reserve one additive edge's absolute observation target.

    Allocation holds ``BEGIN IMMEDIATE`` while reading both the live edge and
    prior reservations in its current validity generation. Therefore delta B
    reserves ``N+2`` even when delta A already reserved ``N+1`` but crashed
    before mutating the edge. A newly reopened edge starts a fresh generation
    at one. The receipt commits before the caller applies ``MAX(target)``, so
    every interruption point is retryable without another additive increment.

    One delta may own only one receipt for a base logical effect across all
    validity generations. If a failed delta retries after that edge was closed
    and reopened, its original receipt is returned with ``should_apply=False``:
    old evidence must not be counted again in the new interval.

    Returns ``(target_observations, current_edge_id, should_apply)``. The edge
    id is refreshed under the same write lock so a same-generation retry does
    not rely on a stale pre-receipt edge scan.
    """
    ensure_schema(conn)
    from . import relation_edges as edges_store

    edges_store.ensure_schema(conn)
    effect = str(effect_key).strip()
    src = str(src_identity).strip()
    dst = str(dst_identity).strip()
    pred = str(predicate).strip()
    if int(delta_id) < 1:
        raise ValueError("memory_delta apply receipt requires a positive delta_id")
    if not effect or not src or not dst or not pred:
        raise ValueError("memory_delta apply receipt edge fields must be non-empty")
    if conn.in_transaction:
        raise RuntimeError("reserve_additive_target requires an autocommit connection")

    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT 1 FROM memory_deltas WHERE id=?", (delta_id,)).fetchone() is None:
            raise ValueError(f"memory_delta {delta_id} does not exist")

        if pred == "knows":
            edge = conn.execute(
                "SELECT edge_id, observations FROM relation_edges"
                " WHERE predicate=? AND valid_to IS NULL"
                " AND status IN ('shadow','active')"
                " AND ((src_identity=? AND dst_identity=?)"
                "      OR (src_identity=? AND dst_identity=?))"
                " ORDER BY observations DESC, created_at ASC LIMIT 1",
                (pred, src, dst, dst, src),
            ).fetchone()
            previous_generation = conn.execute(
                "SELECT edge_id FROM relation_edges"
                " WHERE predicate=? AND valid_to IS NOT NULL"
                " AND ((src_identity=? AND dst_identity=?)"
                "      OR (src_identity=? AND dst_identity=?))"
                " ORDER BY valid_to DESC, created_at DESC LIMIT 1",
                (pred, src, dst, dst, src),
            ).fetchone()
        else:
            edge = conn.execute(
                "SELECT edge_id, observations FROM relation_edges"
                " WHERE src_identity=? AND dst_identity=? AND predicate=?"
                " AND valid_to IS NULL AND status IN ('shadow','active')"
                " ORDER BY observations DESC, created_at ASC LIMIT 1",
                (src, dst, pred),
            ).fetchone()
            previous_generation = conn.execute(
                "SELECT edge_id FROM relation_edges"
                " WHERE src_identity=? AND dst_identity=? AND predicate=?"
                " AND valid_to IS NOT NULL"
                " ORDER BY valid_to DESC, created_at DESC LIMIT 1",
                (src, dst, pred),
            ).fetchone()
        edge_id = str(edge[0]) if edge is not None else None
        current_observations = int(edge[1] or 0) if edge is not None else 0
        generation = str(previous_generation[0]) if previous_generation is not None else "initial"
        scoped_effect = f"{effect}:generation:{generation}"

        # The first receipt permanently binds this delta/effect to one validity
        # generation. A later retry must find it before considering the current
        # generation, otherwise close+reopen can make one delta count twice.
        prefix = f"{effect}:generation:"
        existing = conn.execute(
            "SELECT effect_key, target_observations"
            " FROM memory_delta_apply_receipts"
            " WHERE delta_id=?"
            " AND (effect_key=? OR substr(effect_key, 1, ?)=?)"
            " ORDER BY created_at ASC, effect_key ASC LIMIT 1",
            (delta_id, effect, len(prefix), prefix),
        ).fetchone()
        if existing is not None:
            existing_effect = str(existing[0])
            target = int(existing[1])
            should_apply = existing_effect == scoped_effect
        else:
            receipt_max = conn.execute(
                "SELECT COALESCE(MAX(target_observations), 0)"
                " FROM memory_delta_apply_receipts WHERE effect_key=?",
                (scoped_effect,),
            ).fetchone()
            max_reserved = int(receipt_max[0] or 0) if receipt_max is not None else 0
            target = max(current_observations, max_reserved) + 1
            conn.execute(
                "INSERT INTO memory_delta_apply_receipts"
                " (delta_id, effect_key, target_observations, created_at)"
                " VALUES (?, ?, ?, ?)",
                (
                    delta_id,
                    scoped_effect,
                    target,
                    datetime.now().astimezone().isoformat(),
                ),
            )
            should_apply = True
        conn.commit()
        return target, edge_id if should_apply else None, should_apply
    except Exception:
        conn.rollback()
        raise


def stats(conn: sqlite3.Connection) -> dict:
    """Aggregate the latest attempt for every distinct session window."""
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT session_id, payload, dropped FROM memory_deltas m"
        " WHERE id = (SELECT MAX(id) FROM memory_deltas"
        " WHERE session_id=m.session_id AND window_start=m.window_start"
        " AND window_end=m.window_end)"
    ).fetchall()
    heads = {
        "owner_alias_candidates": 0,
        "entities": 0,
        "assertions": 0,
        "relations": 0,
        "events": 0,
    }
    dropped = 0
    for _sid, payload, drop in rows:
        dropped += int(drop or 0)
        try:
            delta = json.loads(payload)
        except (TypeError, ValueError):
            continue
        for head in heads:
            items = delta.get(head)
            if isinstance(items, list):
                heads[head] += len(items)
    total = conn.execute("SELECT COUNT(*) FROM memory_deltas").fetchone()[0]
    return {
        "rows": int(total),
        "sessions": len({row[0] for row in rows}),
        "heads": heads,
        "dropped_by_gates": dropped,
    }
