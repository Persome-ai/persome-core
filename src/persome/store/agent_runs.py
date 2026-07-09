"""DAO for ``agent_runs`` + ``agent_run_events`` — the canonical ledger of
every agent run shown on the Calendar work board.

Additive table, modeled 1:1 on ``dream_runs``. ``dream_runs`` is left
untouched (no migration runner exists; ``ensure_schema`` runs per-connection,
so an in-place RENAME would crash a second concurrent connection). Phase 1a
only needs reads + a single ``insert_run`` helper; the dispatcher and live
status transitions land in Phase 1b.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..logger import get

logger = get("persome.store.agent_runs")

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL,
    trigger         TEXT NOT NULL,
    dispatch_source TEXT NOT NULL DEFAULT 'system',
    enqueued_at     TEXT NOT NULL,
    started_at      TEXT,
    ended_at        TEXT,
    progress        REAL,
    progress_label  TEXT NOT NULL DEFAULT '',
    eta_seconds     INTEGER,
    payload         TEXT NOT NULL DEFAULT '{}',
    summary         TEXT NOT NULL DEFAULT '',
    result_refs     TEXT NOT NULL DEFAULT '[]',
    error           TEXT NOT NULL DEFAULT '',
    skipped_reason  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_status_enq ON agent_runs(status, enqueued_at);
CREATE INDEX IF NOT EXISTS idx_agent_runs_started    ON agent_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_runs_kind_time  ON agent_runs(kind, enqueued_at DESC);

CREATE TABLE IF NOT EXISTS agent_run_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  INTEGER NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    ts      TEXT NOT NULL,
    type    TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_run_events_run ON agent_run_events(run_id, id);
"""


@dataclass
class AgentRun:
    id: int
    kind: str
    title: str
    status: str
    trigger: str
    dispatch_source: str
    enqueued_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    progress: float | None
    progress_label: str
    eta_seconds: int | None
    summary: str
    result_refs: list[dict[str, Any]]
    error: str
    skipped_reason: str
    payload: dict[str, Any] = field(default_factory=dict)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def insert_run(
    conn: sqlite3.Connection,
    *,
    kind: str,
    title: str,
    status: str,
    trigger: str,
    dispatch_source: str,
    enqueued_at: str,
    started_at: str | None = None,
    ended_at: str | None = None,
    progress: float | None = None,
    progress_label: str = "",
    eta_seconds: int | None = None,
    summary: str = "",
) -> int:
    """Insert one row verbatim and return its id. Phase 1a uses this from tests;
    Phase 1b's enqueue/transition helpers will supersede direct calls."""
    cur = conn.execute(
        """
        INSERT INTO agent_runs (
            kind, title, status, trigger, dispatch_source,
            enqueued_at, started_at, ended_at,
            progress, progress_label, eta_seconds, summary
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            kind,
            title,
            status,
            trigger,
            dispatch_source,
            enqueued_at,
            started_at,
            ended_at,
            progress,
            progress_label,
            eta_seconds,
            summary,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)  # type: ignore[arg-type]


def list_runs_in_window(
    conn: sqlite3.Connection,
    *,
    start: datetime,
    end: datetime,
    statuses: list[str] | None = None,
) -> list[AgentRun]:
    """Runs whose anchor time (``started_at`` if set, else ``enqueued_at``) lies
    in ``[start, end)``. Optionally filtered to ``statuses``. Newest anchor first.

    Window comparison is done in Python on tz-aware datetimes (naive stored
    timestamps are treated as local) to dodge the naive/aware string-comparison
    bug that bites the intent agenda path."""
    rows = conn.execute("SELECT * FROM agent_runs").fetchall()
    local_tz = start.tzinfo
    out: list[AgentRun] = []
    for r in rows:
        run = _row_to_run(r, local_tz)
        anchor = run.started_at or run.enqueued_at
        if not (start <= anchor < end):
            continue
        if statuses is not None and run.status not in statuses:
            continue
        out.append(run)
    out.sort(key=lambda x: x.started_at or x.enqueued_at, reverse=True)
    return out


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _row_to_run(r: sqlite3.Row, local_tz: Any) -> AgentRun:
    def _dt(v: str | None) -> datetime | None:
        if not v:
            return None
        dt = datetime.fromisoformat(v)
        return dt.replace(tzinfo=local_tz) if dt.tzinfo is None else dt

    enq = _dt(r["enqueued_at"])
    assert enq is not None  # NOT NULL in schema
    return AgentRun(
        id=r["id"],
        kind=r["kind"],
        title=r["title"] or "",
        status=r["status"],
        trigger=r["trigger"],
        dispatch_source=r["dispatch_source"] or "system",
        enqueued_at=enq,
        started_at=_dt(r["started_at"]),
        ended_at=_dt(r["ended_at"]),
        progress=r["progress"],
        progress_label=r["progress_label"] or "",
        eta_seconds=r["eta_seconds"],
        summary=r["summary"] or "",
        result_refs=json.loads(r["result_refs"] or "[]"),
        error=r["error"] or "",
        skipped_reason=r["skipped_reason"] or "",
        payload=json.loads(r["payload"] or "{}"),
    )


# ── write-side (Phase 1b) ───────────────────────────────────────────────────


@dataclass
class AgentRunEvent:
    id: int
    run_id: int
    ts: datetime
    type: str
    payload: dict[str, Any]


def find_queued_dup(
    conn: sqlite3.Connection,
    *,
    kind: str,
    payload: dict[str, Any] | None = None,
) -> int | None:
    """Id of an existing still-queued row this enqueue would fold into, or None.

    Dedup is **payload-aware** (#397): a row only dedups when it has the same
    ``kind`` AND an identical ``payload``. For payload-less kinds (dream) this is
    just "same kind". For payload-carrying kinds (bootstrap's ``{deep, exclude}``)
    a *different* selection must NOT fold into a stale queued row — otherwise the
    user's new choice is silently dropped and the run uses the old payload.

    Equality is on the parsed dict (key order, whitespace insensitive), matching
    how the row was stored (``json.dumps`` round-trip)."""
    want = payload or {}
    rows = conn.execute(
        "SELECT id, payload FROM agent_runs WHERE kind = ? AND status = 'queued' "
        "ORDER BY enqueued_at ASC",
        (kind,),
    ).fetchall()
    for row in rows:
        if json.loads(row["payload"] or "{}") == want:
            return int(row["id"])
    return None


def enqueue(
    conn: sqlite3.Connection,
    *,
    kind: str,
    trigger: str,
    dispatch_source: str,
    title: str = "",
    payload: dict[str, Any] | None = None,
) -> int:
    """Insert a queued run, or fold into an existing still-queued row of the same
    kind **and identical payload** (dedup — prevents a double-click from burning N
    LLM runs). A queued row with a *different* payload does not fold; a new row is
    inserted so the user's latest selection wins (#397). Returns the run id
    (existing or new)."""
    existing_id = find_queued_dup(conn, kind=kind, payload=payload)
    if existing_id is not None:
        return existing_id
    cur = conn.execute(
        """
        INSERT INTO agent_runs (kind, title, status, trigger, dispatch_source, enqueued_at, payload)
        VALUES (?, ?, 'queued', ?, ?, ?, ?)
        """,
        (
            kind,
            title,
            trigger,
            dispatch_source,
            _now_iso(),
            json.dumps(payload or {}, ensure_ascii=False),
        ),
    )
    conn.commit()
    rid = int(cur.lastrowid)  # type: ignore[arg-type]
    logger.info("agent_runs: enqueued run %s (kind=%s, trigger=%s)", rid, kind, trigger)
    return rid


def claim_oldest_queued(conn: sqlite3.Connection, *, kind: str) -> int | None:
    """Atomically claim the oldest queued row of ``kind`` (queued→running) and
    return its id, or None if none. The ``WHERE status='queued'`` guard on the
    UPDATE guarantees exactly-once claim under concurrent dispatchers."""
    row = conn.execute(
        "SELECT id FROM agent_runs WHERE kind = ? AND status = 'queued' "
        "ORDER BY enqueued_at ASC LIMIT 1",
        (kind,),
    ).fetchone()
    if row is None:
        return None
    rid = int(row["id"])
    cur = conn.execute(
        "UPDATE agent_runs SET status = 'running', started_at = ? "
        "WHERE id = ? AND status = 'queued'",
        (_now_iso(), rid),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None  # lost the race
    return rid


def mark_running(conn: sqlite3.Connection, run_id: int) -> None:
    conn.execute(
        "UPDATE agent_runs SET status = 'running', started_at = COALESCE(started_at, ?) WHERE id = ?",
        (_now_iso(), run_id),
    )
    conn.commit()


def count_inflight(conn: sqlite3.Connection, *, kind: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM agent_runs WHERE kind = ? AND status = 'running'",
        (kind,),
    ).fetchone()
    return int(row["n"])


def update_progress(
    conn: sqlite3.Connection, run_id: int, *, progress: float | None, progress_label: str
) -> None:
    conn.execute(
        "UPDATE agent_runs SET progress = ?, progress_label = ? WHERE id = ?",
        (progress, progress_label, run_id),
    )
    conn.commit()


def end_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    committed: bool,
    summary: str,
    result_refs: list[dict[str, Any]],
    iterations: int,
    skipped_reason: str = "",
) -> None:
    # progress=1.0 only when the run actually did the work (committed). A skipped
    # run did nothing this cycle — leaving progress NULL keeps the bar honest
    # (indeterminate) instead of showing a fake full bar.
    if committed:
        conn.execute(
            """
            UPDATE agent_runs SET status = 'committed', ended_at = ?, summary = ?,
                result_refs = ?, skipped_reason = ?, progress = 1.0
             WHERE id = ?
            """,
            (
                _now_iso(),
                summary,
                json.dumps(result_refs, ensure_ascii=False),
                skipped_reason,
                run_id,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE agent_runs SET status = 'skipped', ended_at = ?, summary = ?,
                result_refs = ?, skipped_reason = ?
             WHERE id = ?
            """,
            (
                _now_iso(),
                summary,
                json.dumps(result_refs, ensure_ascii=False),
                skipped_reason,
                run_id,
            ),
        )
    conn.commit()


def fail_run(conn: sqlite3.Connection, run_id: int, *, error: str) -> None:
    conn.execute(
        "UPDATE agent_runs SET status = 'failed', ended_at = ?, error = ? WHERE id = ?",
        (_now_iso(), error, run_id),
    )
    conn.commit()


def cancel_run(conn: sqlite3.Connection, run_id: int) -> bool:
    """Cancel a run. A queued row flips straight to 'cancelled'. A running row is
    left for the executor to notice (Phase 1b records the request; cooperative
    stop of an in-flight tool loop is out of scope — running cancel returns False
    so callers know it didn't take immediately)."""
    cur = conn.execute(
        "UPDATE agent_runs SET status = 'cancelled', ended_at = ? WHERE id = ? AND status = 'queued'",
        (_now_iso(), run_id),
    )
    conn.commit()
    return cur.rowcount > 0


def mark_orphans_running(conn: sqlite3.Connection) -> int:
    """On daemon boot: any row still 'running' is from a process that died
    mid-run → 'failed'. **Queued rows are preserved** (legitimately waiting; the
    dispatcher will pick them up)."""
    cur = conn.execute(
        "UPDATE agent_runs SET status = 'failed', ended_at = ?, error = 'daemon restarted' "
        "WHERE status = 'running'",
        (_now_iso(),),
    )
    conn.commit()
    n = int(cur.rowcount)
    if n:
        logger.info("agent_runs: marked %d orphan running rows as failed", n)
    return n


def append_event(
    conn: sqlite3.Connection, run_id: int, event_type: str, payload: dict[str, Any]
) -> None:
    conn.execute(
        "INSERT INTO agent_run_events (run_id, ts, type, payload) VALUES (?, ?, ?, ?)",
        (run_id, _now_iso(), event_type, json.dumps(payload, ensure_ascii=False)),
    )
    conn.commit()


def list_events(conn: sqlite3.Connection, run_id: int) -> list[AgentRunEvent]:
    rows = conn.execute(
        "SELECT * FROM agent_run_events WHERE run_id = ? ORDER BY id ASC", (run_id,)
    ).fetchall()
    return [
        AgentRunEvent(
            id=r["id"],
            run_id=r["run_id"],
            ts=datetime.fromisoformat(r["ts"]),
            type=r["type"],
            payload=json.loads(r["payload"] or "{}"),
        )
        for r in rows
    ]


def get_run(conn: sqlite3.Connection, run_id: int) -> AgentRun | None:
    row = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
    return _row_to_run(row, datetime.now().astimezone().tzinfo) if row else None
