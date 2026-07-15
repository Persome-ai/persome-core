"""SQLite-backed store for timeline blocks (default 1-min wall-clock windows).

Lives in the shared ``index.db`` so users still have one file to back
up. The schema enforces a uniqueness constraint on
``(start_time, end_time)`` so the aggregator tick is idempotent.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..capture.timestamps import capture_timestamp_epoch

SCHEMA = """
CREATE TABLE IF NOT EXISTS timeline_blocks (
    id TEXT PRIMARY KEY,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT '',
    entries TEXT NOT NULL,
    apps_used TEXT NOT NULL,
    capture_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    skill_hints TEXT NOT NULL DEFAULT '[]',
    action_trace TEXT NOT NULL DEFAULT '[]',
    focus_excerpt TEXT NOT NULL DEFAULT '',
    focus_structured TEXT NOT NULL DEFAULT '',
    attention_surface TEXT NOT NULL DEFAULT '',
    attention_confidence REAL NOT NULL DEFAULT 0.0,
    attention_rung TEXT NOT NULL DEFAULT '',
    UNIQUE(start_time, end_time)
);
CREATE INDEX IF NOT EXISTS idx_tlb_start ON timeline_blocks(start_time);
CREATE INDEX IF NOT EXISTS idx_tlb_end ON timeline_blocks(end_time);

CREATE TABLE IF NOT EXISTS skill_observations (
    session_id TEXT NOT NULL,
    skill_path TEXT NOT NULL,
    timeline_block_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (session_id, skill_path)
);
CREATE INDEX IF NOT EXISTS idx_skill_observations_block
    ON skill_observations(timeline_block_id);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Backfill columns added after initial schema."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(timeline_blocks)")}
    if "skill_hints" not in cols:
        conn.execute(
            "ALTER TABLE timeline_blocks ADD COLUMN skill_hints TEXT NOT NULL DEFAULT '[]'"
        )
    if "action_trace" not in cols:
        conn.execute(
            "ALTER TABLE timeline_blocks ADD COLUMN action_trace TEXT NOT NULL DEFAULT '[]'"
        )
    if "focus_excerpt" not in cols:
        conn.execute(
            "ALTER TABLE timeline_blocks ADD COLUMN focus_excerpt TEXT NOT NULL DEFAULT ''"
        )
    if "focus_structured" not in cols:
        conn.execute(
            "ALTER TABLE timeline_blocks ADD COLUMN focus_structured TEXT NOT NULL DEFAULT ''"
        )
    if "attention_surface" not in cols:
        conn.execute(
            "ALTER TABLE timeline_blocks ADD COLUMN attention_surface TEXT NOT NULL DEFAULT ''"
        )
    if "attention_confidence" not in cols:
        conn.execute(
            "ALTER TABLE timeline_blocks ADD COLUMN attention_confidence REAL NOT NULL DEFAULT 0.0"
        )
    if "attention_rung" not in cols:
        conn.execute(
            "ALTER TABLE timeline_blocks ADD COLUMN attention_rung TEXT NOT NULL DEFAULT ''"
        )


@dataclass
class TimelineBlock:
    start_time: datetime
    end_time: datetime
    timezone: str = ""
    entries: list[str] = field(default_factory=list)
    apps_used: list[str] = field(default_factory=list)
    capture_count: int = 0
    id: str = ""
    created_at: datetime | None = None
    skill_hints: list[dict] = field(default_factory=list)
    action_trace: list[dict] = field(default_factory=list)
    # Raw visible_text excerpt of the window's last capture — a lossless backstop
    # for the lossy LLM-normalized ``entries`` (chat apps especially summarize
    # message bodies away). Session modeling reads this for the focus block so
    # a verbatim message (e.g. a counterpart's proposed time) is
    # never lost to normalization.
    focus_excerpt: str = ""
    # Structured conversation produced by a per-app parser (parsers.get_parser),

    # over the raw ``focus_excerpt`` as the modeler's focus input — the parser
    # has already split sender/time/body deterministically, so it is cleaner than
    # the verbatim AX dump. Empty when no parser handles the focused app (the
    # modeler then falls back to ``focus_excerpt``).
    focus_structured: str = ""
    # Attention-locus summary for the window's dominant locus (Step 1 of the
    # attention-locus design). ``surface`` = the window/pane attended; ``rung``
    # = which fusion-ladder rung won (pane/editing/cursor/focus/fallback);
    # ``confidence`` 0..1. Code-computed at aggregation; what the Phase-5 oracle
    # scores and Step-2 consumers will read. Empty/0.0 on old rows + when the
    # locus flag is off.
    attention_surface: str = ""
    attention_confidence: float = 0.0
    attention_rung: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = _make_id(self.start_time)
        if self.created_at is None:
            self.created_at = datetime.now().astimezone()


def _make_id(start: datetime) -> str:
    stamp = start.strftime("%Y%m%d-%H%M")
    suffix = hashlib.blake2s(os.urandom(8), digest_size=2).hexdigest()
    return f"tlb-{stamp}-{suffix}"


def ensure_schema(conn: sqlite3.Connection) -> None:
    from ..store import fts

    if fts.is_client_process():
        return
    conn.create_function("persome_epoch", 1, capture_timestamp_epoch)
    conn.executescript(SCHEMA)
    _migrate(conn)


def has_window(conn: sqlite3.Connection, start: datetime, end: datetime) -> bool:
    row = conn.execute(
        "SELECT 1 FROM timeline_blocks "
        "WHERE persome_epoch(start_time)=persome_epoch(?) "
        "AND persome_epoch(end_time)=persome_epoch(?) LIMIT 1",
        (start.isoformat(), end.isoformat()),
    ).fetchone()
    return row is not None


def insert(conn: sqlite3.Connection, block: TimelineBlock) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO timeline_blocks
            (id, start_time, end_time, timezone, entries, apps_used, capture_count,
             created_at, skill_hints, action_trace, focus_excerpt,
             focus_structured, attention_surface, attention_confidence, attention_rung)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            block.id,
            block.start_time.isoformat(),
            block.end_time.isoformat(),
            block.timezone,
            json.dumps(block.entries, ensure_ascii=False),
            json.dumps(block.apps_used, ensure_ascii=False),
            block.capture_count,
            (block.created_at or datetime.now().astimezone()).isoformat(),
            json.dumps(block.skill_hints, ensure_ascii=False),
            json.dumps(block.action_trace, ensure_ascii=False),
            block.focus_excerpt,
            block.focus_structured,
            block.attention_surface,
            block.attention_confidence,
            block.attention_rung,
        ),
    )


def claim_skill_observation(
    conn: sqlite3.Connection,
    *,
    block: TimelineBlock,
    skill_path: str,
) -> bool:
    """Claim one durable skill observation per session.

    Timeline classification runs once per minute, while behavioral evidence is
    session-scoped. Without this gate a long session can append the same skill
    echo dozens of times and make one continuous episode look like independent
    support. Minute blocks are wall-clock aligned while sessions may begin at
    any second, so associate by interval overlap rather than requiring the
    block start to fall inside the session. If no overlapping session exists,
    preserve the legacy best-effort echo rather than silently dropping the
    observation.
    """
    row = conn.execute(
        """
        SELECT id
          FROM sessions
         WHERE persome_epoch(start_time) < persome_epoch(?)
           AND (end_time IS NULL OR persome_epoch(end_time) > persome_epoch(?))
         ORDER BY persome_epoch(start_time) DESC
         LIMIT 1
        """,
        (block.end_time.isoformat(), block.start_time.isoformat()),
    ).fetchone()
    if row is None:
        return True

    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO skill_observations
            (session_id, skill_path, timeline_block_id, observed_at)
        VALUES (?, ?, ?, ?)
        """,
        (row["id"], skill_path, block.id, block.start_time.isoformat()),
    )
    return cursor.rowcount == 1


def get_latest_end(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute(
        "SELECT end_time FROM timeline_blocks ORDER BY persome_epoch(end_time) DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    try:
        return datetime.fromisoformat(row[0])
    except (TypeError, ValueError):
        return None


def query_recent(conn: sqlite3.Connection, *, limit: int = 12) -> list[TimelineBlock]:
    """Most recent blocks, oldest first in the returned list."""
    rows = conn.execute(
        "SELECT * FROM timeline_blocks ORDER BY persome_epoch(start_time) DESC LIMIT ?",
        (limit,),
    ).fetchall()
    blocks = [_row_to_block(r) for r in rows]
    blocks.reverse()
    return blocks


def query_since(conn: sqlite3.Connection, since: datetime) -> list[TimelineBlock]:
    """All blocks with end_time > ``since``, chronological order."""
    rows = conn.execute(
        "SELECT * FROM timeline_blocks "
        "WHERE persome_epoch(end_time) > persome_epoch(?) "
        "ORDER BY persome_epoch(start_time) ASC",
        (since.isoformat(),),
    ).fetchall()
    return [_row_to_block(r) for r in rows]


def query_range(
    conn: sqlite3.Connection,
    since: datetime | None,
    until: datetime | None,
    limit: int = 50,
) -> list[TimelineBlock]:
    """Query blocks within optional time bounds, newest first."""
    clauses: list[str] = []
    params: list[str | int] = []
    if since:
        clauses.append("persome_epoch(start_time) >= persome_epoch(?)")
        params.append(since.isoformat())
    if until:
        clauses.append("persome_epoch(end_time) <= persome_epoch(?)")
        params.append(until.isoformat())
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(min(limit, 200))
    rows = conn.execute(
        f"SELECT * FROM timeline_blocks {where} "  # noqa: S608
        "ORDER BY persome_epoch(start_time) DESC LIMIT ?",
        params,
    ).fetchall()
    return [_row_to_block(r) for r in rows]


def query_overlapping(
    conn: sqlite3.Connection,
    window_start: datetime,
    window_end: datetime,
    limit: int = 50,
) -> list[TimelineBlock]:
    """Nearest blocks overlapping ``[window_start, window_end)``, chronological.

    Timeline windows are half-open throughout the Runtime.  Use strict overlap
    (``end_time > window_start AND start_time < window_end``), not the
    containment ``query_range`` applies: a block straddling a boundary counts,
    while one that merely touches a boundary does not.

    The interval midpoint is the association anchor.  When more rows overlap
    than ``limit``, retain the rows nearest that anchor, then return that subset
    in chronological order.  Taking the first chronological rows would bias a
    symmetric window toward its oldest edge and could omit the anchor itself.
    """
    start_epoch = window_start.timestamp()
    end_epoch = window_end.timestamp()
    if end_epoch <= start_epoch:
        return []
    anchor_epoch = start_epoch + ((end_epoch - start_epoch) / 2.0)
    rows = conn.execute(
        """
        WITH candidates AS MATERIALIZED (
            SELECT *,
                   persome_epoch(start_time) AS _start_epoch,
                   persome_epoch(end_time) AS _end_epoch
              FROM timeline_blocks
             WHERE persome_epoch(end_time) > ?
               AND persome_epoch(start_time) < ?
        ),
        nearest AS (
            SELECT *
              FROM candidates
             ORDER BY
                   CASE
                       WHEN _end_epoch <= ? THEN ? - _end_epoch
                       WHEN _start_epoch >= ? THEN _start_epoch - ?
                       ELSE 0.0
                   END ASC,
                   _start_epoch ASC,
                   id ASC
             LIMIT ?
        )
        SELECT * FROM nearest ORDER BY _start_epoch ASC, id ASC
        """,
        (
            start_epoch,
            end_epoch,
            anchor_epoch,
            anchor_epoch,
            anchor_epoch,
            anchor_epoch,
            max(0, min(limit, 200)),
        ),
    ).fetchall()
    return [_row_to_block(r) for r in rows]


def _row_to_block(row: sqlite3.Row | tuple) -> TimelineBlock:
    # Row indexing works for both sqlite3.Row and tuple
    get = row.__getitem__
    try:
        skill_hints_raw = get("skill_hints")  # type: ignore[call-overload]
    except (IndexError, KeyError):
        skill_hints_raw = "[]"
    try:
        action_trace_raw = get("action_trace")  # type: ignore[call-overload]
    except (IndexError, KeyError):
        action_trace_raw = "[]"
    try:
        focus_excerpt = get("focus_excerpt") or ""  # type: ignore[call-overload]
    except (IndexError, KeyError):
        focus_excerpt = ""
    try:
        focus_structured = get("focus_structured") or ""  # type: ignore[call-overload]
    except (IndexError, KeyError):
        focus_structured = ""
    try:
        attention_surface = get("attention_surface") or ""  # type: ignore[call-overload]
    except (IndexError, KeyError):
        attention_surface = ""
    try:
        attention_confidence = float(get("attention_confidence") or 0.0)  # type: ignore[call-overload]
    except (IndexError, KeyError, TypeError, ValueError):
        attention_confidence = 0.0
    try:
        attention_rung = get("attention_rung") or ""  # type: ignore[call-overload]
    except (IndexError, KeyError):
        attention_rung = ""
    return TimelineBlock(
        id=get("id"),  # type: ignore[call-overload]
        start_time=datetime.fromisoformat(get("start_time")),  # type: ignore[call-overload]
        end_time=datetime.fromisoformat(get("end_time")),  # type: ignore[call-overload]
        timezone=get("timezone") or "",  # type: ignore[call-overload]
        entries=json.loads(get("entries") or "[]"),  # type: ignore[call-overload]
        apps_used=json.loads(get("apps_used") or "[]"),  # type: ignore[call-overload]
        capture_count=get("capture_count") or 0,  # type: ignore[call-overload]
        created_at=datetime.fromisoformat(get("created_at")) if get("created_at") else None,  # type: ignore[call-overload]
        skill_hints=json.loads(skill_hints_raw or "[]"),
        action_trace=json.loads(action_trace_raw or "[]"),
        focus_excerpt=focus_excerpt,
        focus_structured=focus_structured,
        attention_surface=attention_surface,
        attention_confidence=attention_confidence,
        attention_rung=attention_rung,
    )


def floor_to_window(moment: datetime, window_minutes: int) -> datetime:
    """Floor to the wall-clock window boundary. 14:07:42 → 14:05:00 (w=5)."""
    floor_min = (moment.minute // window_minutes) * window_minutes
    return moment.replace(minute=floor_min, second=0, microsecond=0)


def iter_windows(
    start: datetime, end: datetime, window_minutes: int
) -> list[tuple[datetime, datetime]]:
    """Return the list of complete closed windows in ``[start, end)``.

    ``start`` is floored first; windows that would extend past ``end`` are
    not returned (partial trailing windows are left for a later tick).
    """
    cursor = floor_to_window(start, window_minutes)
    if cursor < start:
        cursor = start
    step = timedelta(minutes=window_minutes)
    out: list[tuple[datetime, datetime]] = []
    while cursor + step <= end:
        out.append((cursor, cursor + step))
        cursor = cursor + step
    return out
