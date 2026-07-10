"""SQLite FTS5 index for fast BM25 search over memory entries."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import paths
from ..logger import get

logger = get("persome.store")

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS entries USING fts5(
    id UNINDEXED,
    path UNINDEXED,
    prefix UNINDEXED,
    timestamp UNINDEXED,
    tags,
    content,
    superseded UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    prefix TEXT,
    description TEXT,
    tags TEXT,
    status TEXT,
    entry_count INTEGER,
    created TEXT,
    updated TEXT,
    needs_compact INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_prefix ON files(prefix);

-- Mirrors capture-buffer/*.json S1 fields for keyword search. The JSON file on
-- disk stays authoritative for screenshots (not duplicated here). Populated
-- write-through from capture/scheduler; rows removed by cleanup_buffer when the
-- JSON is deleted. Screenshot-strip leaves this untouched (text unchanged).
CREATE TABLE IF NOT EXISTS captures (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT UNIQUE NOT NULL,
    timestamp TEXT NOT NULL,
    app_name TEXT,
    bundle_id TEXT,
    window_title TEXT,
    focused_role TEXT,
    focused_value TEXT,
    visible_text TEXT,
    url TEXT
);

CREATE INDEX IF NOT EXISTS idx_captures_ts  ON captures(timestamp);
CREATE INDEX IF NOT EXISTS idx_captures_app ON captures(app_name);

CREATE VIRTUAL TABLE IF NOT EXISTS captures_fts USING fts5(
    app_name, window_title, focused_value, visible_text, url,
    content='captures', content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS captures_ai AFTER INSERT ON captures BEGIN
    INSERT INTO captures_fts(rowid, app_name, window_title, focused_value, visible_text, url)
    VALUES (new.rowid, new.app_name, new.window_title, new.focused_value, new.visible_text, new.url);
END;
CREATE TRIGGER IF NOT EXISTS captures_ad AFTER DELETE ON captures BEGIN
    INSERT INTO captures_fts(captures_fts, rowid, app_name, window_title, focused_value, visible_text, url)
    VALUES ('delete', old.rowid, old.app_name, old.window_title, old.focused_value, old.visible_text, old.url);
END;
CREATE TRIGGER IF NOT EXISTS captures_au AFTER UPDATE ON captures BEGIN
    INSERT INTO captures_fts(captures_fts, rowid, app_name, window_title, focused_value, visible_text, url)
    VALUES ('delete', old.rowid, old.app_name, old.window_title, old.focused_value, old.visible_text, old.url);
    INSERT INTO captures_fts(rowid, app_name, window_title, focused_value, visible_text, url)
    VALUES (new.rowid, new.app_name, new.window_title, new.focused_value, new.visible_text, new.url);
END;

-- (OCR is now on-device & synchronous: results are backfilled straight into
-- captures.visible_text, so the former async ocr_jobs table was retired. Old DBs
-- keep the orphaned table — harmless, never read.)

-- Per-entry retrieval stats. Lives in a regular table because the entries
-- table is FTS5 (no ALTER TABLE). Rows are created lazily on first hit
-- via INSERT ... ON CONFLICT; missing row == count 0. Feeds the
-- consolidation layer's load-bearing vs redundant entry detection.
CREATE TABLE IF NOT EXISTS entry_retrieval_stats (
    entry_id TEXT PRIMARY KEY,
    retrieval_count INTEGER NOT NULL DEFAULT 0,
    last_retrieved_at TEXT
);

-- markdown 投影态（evomem SSOT 切换 PR-6b，write_authority="evomem" 专用）。
-- 每次反转写口成功把投影写进 live memory/ 后记录其内容 hash；daemon 的
-- manual-edit 检测用「当前文件 hash ≠ 上次投影 hash」识别手改（Q1 裁定 (b)：
-- 报警 + 提供 evomem-import-markdown 回灌，不做自动回灌）。投影写失败不更新
-- 本表（滞后 ≠ 手改，靠 markdown_projection_lag 计数器另行报警）。
CREATE TABLE IF NOT EXISTS projection_state (
    file_name    TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    projected_at TEXT NOT NULL
);
"""


@dataclass
class EntryHit:
    id: str
    path: str
    timestamp: str
    content: str
    rank: float


@dataclass
class FileRow:
    path: str
    prefix: str
    description: str
    tags: str
    status: str
    entry_count: int
    created: str
    updated: str
    needs_compact: int


def _ensure_entry_temporal(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS entry_temporal (
            entry_id   TEXT PRIMARY KEY,
            valid_from TEXT NOT NULL,
            valid_until TEXT
        );
        INSERT OR IGNORE INTO entry_temporal(entry_id, valid_from, valid_until)
            SELECT id, timestamp, NULL FROM entries WHERE superseded=0;
        INSERT OR IGNORE INTO entry_temporal(entry_id, valid_from, valid_until)
            SELECT id, timestamp, timestamp FROM entries WHERE superseded=1;
    """)


# ─── entry_metadata (meta-cognition layer, Hy-Memory migration) ───────────────
# Per-entry reliability metadata: how trustworthy a memory is, whether it
# conflicts with another belief, and when the underlying event actually
# happened (distinct from the write-time ``timestamp``). Regular table because
# ``entries`` is FTS5 (no ALTER TABLE) — same旁挂 pattern as ``entry_temporal``.
# These columns are a pure projection of the heading colon-tags
# ``#confidence:<level>`` / ``#conflicted`` / ``#occurred:<iso>``, rebuilt
# wholesale by ``rebuild_index``.
#
# INVARIANT: a row exists IFF the entry carries at least
# one non-default metadata tag. ``set_entry_metadata`` is the single writer for
# both the incremental path (append/supersede) and the rebuild replay, so the
# incremental table is byte-for-byte equal to a fresh rebuild.
CONFIDENCE_LEVELS = ("high", "medium", "low")


def _ensure_entry_metadata(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS entry_metadata (
            entry_id    TEXT PRIMARY KEY,
            confidence  TEXT,
            conflicted  INTEGER NOT NULL DEFAULT 0,
            occurred_at TEXT
        );
    """)


def _norm_confidence(value: str | None) -> str | None:
    """Normalize a confidence level; unknown / empty values collapse to NULL."""
    if not value:
        return None
    v = value.strip().lower()
    return v if v in CONFIDENCE_LEVELS else None


def set_entry_metadata(
    conn: sqlite3.Connection,
    entry_id: str,
    *,
    confidence: str | None = None,
    conflicted: bool = False,
    occurred_at: str | None = None,
) -> None:
    """Upsert one entry's meta-cognition row, or delete it when all-default.

    Keeping the all-default case row-less is what makes the incremental table
    equal a fresh rebuild: a rebuild only emits a row for an entry whose
    markdown carries a metadata tag, so the incremental path must do the same.
    """
    conf = _norm_confidence(confidence)
    occurred = occurred_at or None
    if conf is None and not conflicted and occurred is None:
        conn.execute("DELETE FROM entry_metadata WHERE entry_id=?", (entry_id,))
        return
    conn.execute(
        """
        INSERT INTO entry_metadata(entry_id, confidence, conflicted, occurred_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(entry_id) DO UPDATE SET
            confidence=excluded.confidence,
            conflicted=excluded.conflicted,
            occurred_at=excluded.occurred_at
        """,
        (entry_id, conf, 1 if conflicted else 0, occurred),
    )


def get_entry_metadata(conn: sqlite3.Connection, entry_id: str) -> dict[str, Any] | None:
    """Return ``{confidence, conflicted, occurred_at}`` for one entry, or None."""
    r = conn.execute(
        "SELECT confidence, conflicted, occurred_at FROM entry_metadata WHERE entry_id=?",
        (entry_id,),
    ).fetchone()
    if r is None:
        return None
    return {
        "confidence": r["confidence"],
        "conflicted": bool(r["conflicted"]),
        "occurred_at": r["occurred_at"],
    }


def entry_metadata_map(
    conn: sqlite3.Connection, entry_ids: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """Batch fetch metadata for many entries (recall renders many hits at once)."""
    ids = [e for e in entry_ids if e]
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT entry_id, confidence, conflicted, occurred_at "
        f"FROM entry_metadata WHERE entry_id IN ({placeholders})",
        ids,
    ).fetchall()
    return {
        r["entry_id"]: {
            "confidence": r["confidence"],
            "conflicted": bool(r["conflicted"]),
            "occurred_at": r["occurred_at"],
        }
        for r in rows
    }


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    db_path = db_path or paths.index_db()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    # Make the auto-checkpoint pages explicit (this is also the SQLite default).
    # Auto-checkpoint resets the WAL pointer but never shrinks the file —
    # the daemon calls ``checkpoint()`` from the daily tick so the
    # ``.db-wal`` and ``.db-shm`` sidecars don't drift unbounded.
    conn.execute("PRAGMA wal_autocheckpoint=1000")
    conn.executescript(SCHEMA)
    from ..intent import store as intent_store_mod
    from ..session import store as session_store
    from ..timeline import store as timeline_store

    timeline_store.ensure_schema(conn)
    session_store.ensure_schema(conn)
    intent_store_mod.ensure_schema(conn)
    _ensure_entry_temporal(conn)
    _ensure_entry_metadata(conn)
    from . import vectors as vectors_mod

    vectors_mod.ensure_schema(conn)
    return conn


@contextmanager
def cursor(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def checkpoint(mode: str = "TRUNCATE") -> tuple[int, int, int]:
    """Run ``PRAGMA wal_checkpoint(<mode>)`` and return (busy, log, checkpointed).

    ``TRUNCATE`` is the form that actually shrinks the ``.db-wal`` sidecar;
    ``PASSIVE`` (default in auto-checkpoint) only advances the read pointer
    without touching the file. Best invoked from a periodic tick when the
    daemon is otherwise quiet so we don't fight active readers.
    """
    valid = ("PASSIVE", "FULL", "RESTART", "TRUNCATE")
    mode = mode.upper()
    if mode not in valid:
        raise ValueError(f"invalid checkpoint mode {mode!r}; expected one of {valid}")
    with cursor() as conn:
        row = conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
        if row is None:
            return (0, 0, 0)
        return (int(row[0]), int(row[1]), int(row[2]))


# ─── files table ───────────────────────────────────────────────────────────


def upsert_file(conn: sqlite3.Connection, row: FileRow) -> None:
    conn.execute(
        """
        INSERT INTO files(path, prefix, description, tags, status, entry_count,
                          created, updated, needs_compact)
        VALUES (:path, :prefix, :description, :tags, :status, :entry_count,
                :created, :updated, :needs_compact)
        ON CONFLICT(path) DO UPDATE SET
            prefix=excluded.prefix,
            description=excluded.description,
            tags=excluded.tags,
            status=excluded.status,
            entry_count=excluded.entry_count,
            created=excluded.created,
            updated=excluded.updated,
            needs_compact=excluded.needs_compact
        """,
        row.__dict__,
    )


def get_file(conn: sqlite3.Connection, path: str) -> FileRow | None:
    r = conn.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
    return _to_file_row(r) if r else None


def list_files(
    conn: sqlite3.Connection,
    *,
    include_dormant: bool = False,
    include_archived: bool = False,
) -> list[FileRow]:
    statuses = ["active"]
    if include_dormant:
        statuses.append("dormant")
    if include_archived:
        statuses.append("archived")
    placeholders = ",".join("?" * len(statuses))
    rows = conn.execute(
        f"SELECT * FROM files WHERE status IN ({placeholders}) ORDER BY updated DESC",
        statuses,
    ).fetchall()
    return [_to_file_row(r) for r in rows]


def set_needs_compact(conn: sqlite3.Connection, path: str, value: bool) -> None:
    conn.execute("UPDATE files SET needs_compact=? WHERE path=?", (1 if value else 0, path))


def files_needing_compact(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT path FROM files WHERE needs_compact=1").fetchall()
    return [r["path"] for r in rows]


def _to_file_row(r: sqlite3.Row) -> FileRow:
    return FileRow(
        path=r["path"],
        prefix=r["prefix"] or "",
        description=r["description"] or "",
        tags=r["tags"] or "",
        status=r["status"] or "active",
        entry_count=r["entry_count"] or 0,
        created=r["created"] or "",
        updated=r["updated"] or "",
        needs_compact=r["needs_compact"] or 0,
    )


# ─── entries (FTS5) ────────────────────────────────────────────────────────


def insert_entry(
    conn: sqlite3.Connection,
    *,
    id: str,
    path: str,
    prefix: str,
    timestamp: str,
    tags: str,
    content: str,
    superseded: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO entries(id, path, prefix, timestamp, tags, content, superseded)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (id, path, prefix, timestamp, tags, content, superseded),
    )


def mark_superseded(conn: sqlite3.Connection, entry_id: str) -> None:
    conn.execute("UPDATE entries SET superseded=1 WHERE id=?", (entry_id,))


def delete_entries_for(conn: sqlite3.Connection, path: str) -> None:
    conn.execute("DELETE FROM entries WHERE path=?", (path,))


def delete_file_row(conn: sqlite3.Connection, path: str) -> None:
    conn.execute("DELETE FROM files WHERE path=?", (path,))


_FTS5_SPECIALS = set('":*()^+-')

# 轴A 匹配面 (issue #557): the FTS5 ``entries`` table indexes the ``tags`` column, so a
# bare MATCH also hits classification LABELS (#intent #kind:meeting schema fact entity …).
# On the real store, 251 live hits for the token 'intent' contained it ONLY in their tags —
# recognizer CANDIDATE rows recalled by their label, not their content (47 of them have an
# EMPTY content and are reachable through no other route). Default False restricts matching
# to the content column via an FTS5 ``{content}:`` filter — zero migration, read-side only;
# True is the kill-switch back to label-matchable. Wired from ``[search] tags_matchable``.
_MATCH: dict[str, Any] = {"tags_matchable": False}


def set_tags_matchable(enabled: bool) -> None:
    _MATCH["tags_matchable"] = bool(enabled)


def _safe_fts_query(query: str, *, restrict_to_content: bool = False) -> str:
    """Turn a (possibly LLM- or question-shaped) query into a safe FTS5 MATCH expr.

    FTS5 treats ``col:value``, quotes, parens, etc. as syntax. An LLM that
    writes ``"interview 20:00"`` otherwise crashes the search. We tokenize on
    whitespace, strip special chars from each token, wrap every surviving token
    as a quoted phrase, and join them with **OR** so ``bm25()`` ranking decides
    relevance (best-matching entries first), instead of requiring EVERY token to
    appear.

    Why OR, not the old implicit-AND: AND demands every token be present, so a
    natural-language query ("What breed is my dog Mochi?") returns NOTHING when
    its filler words ("What", "breed", "is") are absent from the stored text —
    even though the entry that names the dog's breed is right there. Measured on
    LongMemEval, implicit-AND retrieved the evidence session for only ~8.5% of
    questions (0% at _m scale); OR + bm25 ranking lifts that to ~0.99 on the same
    store (spec docs/superpowers/specs/2026-06-22-longmemeval-integration-design.md).
    Single-term queries are unaffected (one token → no OR). ``search`` already
    ``ORDER BY bm25(entries)``, so OR does not flood results — it widens the
    candidate set and lets rank do the selection.
    """
    tokens: list[str] = []
    for raw in query.split():
        cleaned = "".join(c for c in raw if c not in _FTS5_SPECIALS)
        if cleaned:
            tokens.append(f'"{cleaned}"')
    if not tokens:
        return '""'
    expr = " OR ".join(tokens)
    if restrict_to_content and not _MATCH["tags_matchable"]:
        # 轴A: match CONTENT only — classification labels (the ``entries.tags``
        # column) stop being matchable text, so a query token like "meeting"/
        # "intent" can no longer recall a candidate row by its label (#557).
        # ONLY the entries table has a content column — captures_fts callers
        # (app_name/window_title/url are legitimately matchable there) must
        # not set ``restrict_to_content``.
        return "{content}: (" + expr + ")"
    return expr


def increment_retrieval_counts(conn: sqlite3.Connection, entry_ids: Iterable[str]) -> None:
    """Bump retrieval_count and last_retrieved_at for the given entry ids.

    No-op when ``entry_ids`` is empty. Rows are created on first hit via
    ``INSERT ... ON CONFLICT``; entries that have never been retrieved have
    no row (semantically count == 0).
    """
    ids = [eid for eid in entry_ids if eid]
    if not ids:
        return
    now = datetime.now(UTC).isoformat()
    conn.executemany(
        """
        INSERT INTO entry_retrieval_stats(entry_id, retrieval_count, last_retrieved_at)
        VALUES (?, 1, ?)
        ON CONFLICT(entry_id) DO UPDATE SET
            retrieval_count = retrieval_count + 1,
            last_retrieved_at = excluded.last_retrieved_at
        """,
        [(eid, now) for eid in ids],
    )


def get_retrieval_count(conn: sqlite3.Connection, entry_id: str) -> int:
    """Return the recorded retrieval count for an entry, or 0 if untracked."""
    row = conn.execute(
        "SELECT retrieval_count FROM entry_retrieval_stats WHERE entry_id=?",
        (entry_id,),
    ).fetchone()
    return int(row["retrieval_count"]) if row else 0


def _bm25_pool(
    conn: sqlite3.Connection,
    *,
    query: str,
    path_patterns: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    top_k: int = 5,
    include_superseded: bool = False,
) -> list[EntryHit]:
    """BM25 candidate pool (no retrieval-count side effect). Shared by ``search``
    (which increments) and ``search_hybrid`` (which increments only the fused top-k)."""
    safe_query = _safe_fts_query(query, restrict_to_content=True)
    if not safe_query or safe_query == '""':
        return []
    clauses = ["entries MATCH ?"]
    args: list[Any] = [safe_query]
    if path_patterns:
        path_patterns = [p for p in path_patterns if p]
        if path_patterns:
            path_clauses = []
            for pat in path_patterns:
                path_clauses.append("path GLOB ?")
                args.append(pat)
            clauses.append("(" + " OR ".join(path_clauses) + ")")
    if since:
        clauses.append("timestamp >= ?")
        args.append(since)
    if until:
        clauses.append("timestamp <= ?")
        args.append(until)
    if not include_superseded:
        clauses.append("superseded = 0")

    sql = (
        "SELECT id, path, timestamp, content, bm25(entries) AS rank "
        "FROM entries WHERE " + " AND ".join(clauses) + " ORDER BY rank LIMIT ?"
    )
    args.append(top_k)
    rows = conn.execute(sql, args).fetchall()
    return [
        EntryHit(
            id=r["id"],
            path=r["path"],
            timestamp=r["timestamp"],
            content=r["content"],
            rank=r["rank"],
        )
        for r in rows
    ]


def search(
    conn: sqlite3.Connection,
    *,
    query: str,
    path_patterns: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    top_k: int = 5,
    include_superseded: bool = False,
) -> list[EntryHit]:
    hits = _bm25_pool(
        conn,
        query=query,
        path_patterns=path_patterns,
        since=since,
        until=until,
        top_k=top_k,
        include_superseded=include_superseded,
    )
    increment_retrieval_counts(conn, (h.id for h in hits))
    return hits


# ─── hybrid (BM25 ⊕ dense) retrieval — production hybrid-retrieval spec Phase 2 ───

# Read-side gate, set at daemon boot from cfg.search (mirrors vectors.set_enabled on the
# write side). Default off → search_hybrid delegates to the BM25 search byte-for-byte.
_HYBRID: dict[str, Any] = {"enabled": False, "recall_n": 50, "rrf_k": 20}
# §3.3 RRF pool weights (memory-rebuild §7-3 cutover finding, PR #504): equal-weight
# fusion lets the slot contains-pools (entity/scene/window/relation — up to recall_n
# ids each) out-vote the text heads whenever a query INCIDENTALLY names a roster
# entity, diluting an already-correct bm25+dense ranking (slotted-bucket regression
# −4~−9pp on the real store). The text heads stay the ranking BACKBONE (weight 1.0);
# slot heads vote with these weights — 1.0 restores legacy equal-weight fusion.
# Defaults mirror [search] slot_pool_weight/relation_pool_weight (0.3 = the
# 2026-07-03 parity point (slot=1.0 regressed slotted -6.9pp). relation=1.0 is
# the same-day SS7-8 weight-tuning verdict: the production relation-probe scan
# (12 real hop-queries) reads 7/12 at rel=1.0 vs 4/12 text baseline, while the
# auto-golden regression sweep is BYTE-IDENTICAL from rel=0.0 to 1.0 -- the
# relation pool never perturbs non-relational queries. Weak dominance, GO.
_POOL_WEIGHTS: dict[str, Any] = {
    "slot": 0.3,
    "relation": 1.0,
    "relation_shadow": True,
    "contains_rerank": True,
}


def set_hybrid_config(*, enabled: bool, recall_n: int, rrf_k: int) -> None:
    _HYBRID["enabled"] = bool(enabled)
    _HYBRID["recall_n"] = max(1, int(recall_n))
    _HYBRID["rrf_k"] = max(1, int(rrf_k))


def set_contains_rerank(enabled: bool) -> None:
    """§7-10: dense re-rank of the contains pools (entity/scene/relation)
    before RRF — replaces per-needle recency order with query-cosine order.
    Kill-switch restores recency."""
    _POOL_WEIGHTS["contains_rerank"] = bool(enabled)


def set_relation_shadow(enabled: bool) -> None:
    """§7-3 gain unlock: let audited-clean SHADOW edges join relation-head
    traversal (downweighted ×0.5). Default OFF — flipped by config
    ``[search] relation_include_shadow`` once the sweep verdict clears."""
    _POOL_WEIGHTS["relation_shadow"] = bool(enabled)


def set_pool_weights(*, slot: float, relation: float) -> None:
    """Configure the associative entrance's slot-head vote weights (1.0 = legacy)."""
    _POOL_WEIGHTS["slot"] = max(0.0, float(slot))
    _POOL_WEIGHTS["relation"] = max(0.0, float(relation))


# ─── 轴B recency decay (issue #557) ────────────────────────────────────────
# The RRF fusion is rank-only — the text backbone (BM25 + dense) is time-BLIND, so a
# 3-week-old "we ship 0.3.9" fact outranks yesterday's "we ship 0.4.x" whenever it
# matches the query slightly better, and a recap agent reports the stale one as current.
# Fix: after fusion, each candidate's rank score (1/(rank+1), the same shape the MMR
# re-rank uses) is multiplied by a half-life decay on its entry age and the list is
# re-sorted (stable — ties keep fused order). The FLOOR keeps old-but-most-relevant
# durable facts competitive: anything older than ~2.3 half-lives claps to the floor,
# which also means an age-uniform candidate set keeps its order byte-identical.
# Anchored at ``until`` when the caller passed one (as-of queries decay relative to
# their own clock), else at the NEWEST candidate's timestamp — never the wall clock,
# so the re-rank is a pure function of the store and frozen-baseline gates never
# drift as fixtures age. half_life_days <= 0 disables (byte-identical to pre-#557).
# Wired from ``[search] recency_half_life_days`` / ``recency_decay_floor`` at boot.
_RECENCY: dict[str, Any] = {"half_life_days": 14.0, "floor": 0.2}


def set_recency_decay(*, half_life_days: float, floor: float) -> None:
    _RECENCY["half_life_days"] = float(half_life_days)
    _RECENCY["floor"] = min(1.0, max(0.0, float(floor)))


def _parse_ts(value: str | None) -> datetime | None:
    """Lenient ISO parse → naive local datetime (entry timestamps are naive local)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _apply_recency(
    conn: sqlite3.Connection,
    ids: list[str],
    *,
    until: str | None = None,
) -> list[str]:
    """Re-rank a fused candidate id list by rank score × recency decay.

    The anchor is ``until`` when the caller passed one (as-of queries decay
    relative to their own clock), else the NEWEST candidate's timestamp —
    never the wall clock. Ages are relative *within the candidate set*, so the
    result is a pure function of the store (frozen-baseline gates stay exactly
    reproducible forever; fixtures don't "age" across decay bands as the
    calendar advances). Fail-open: decay disabled, no parseable timestamps, or
    a missing per-entry timestamp (factor 1.0 — neutral) never drops a
    candidate, only reorders. Membership is NEVER changed.
    """
    half_life = float(_RECENCY["half_life_days"])
    if half_life <= 0.0 or len(ids) < 2:
        return ids
    floor = float(_RECENCY["floor"])
    rows = conn.execute(
        "SELECT id, timestamp FROM entries WHERE id IN (" + ",".join("?" * len(ids)) + ")",
        ids,
    ).fetchall()
    ts_by_id = {r["id"]: _parse_ts(r["timestamp"]) for r in rows}
    anchor = _parse_ts(until)
    if anchor is None:
        parsed = [t for t in ts_by_id.values() if t is not None]
        if not parsed:
            return ids
        anchor = max(parsed)
    scored: list[tuple[float, int, str]] = []
    for rank, eid in enumerate(ids):
        ts = ts_by_id.get(eid)
        factor = 1.0
        if ts is not None:
            age_days = max(0.0, (anchor - ts).total_seconds() / 86400.0)
            factor = max(floor, 0.5 ** (age_days / half_life))
        scored.append((-(1.0 / (rank + 1)) * factor, rank, eid))
    scored.sort()
    return [eid for _s, _r, eid in scored]


def hybrid_enabled() -> bool:
    return bool(_HYBRID["enabled"])


def wire_read_path(cfg: Any) -> None:
    """Wire EVERY module-level read gate from config — the one entrance for the
    满血版 read path (#557 design principle).

    DESIGN PRINCIPLE: an MCP-side caller must get the FULL-POWER memory — the
    same hybrid dense ⊕ associative heads ⊕ recency decay ⊕ content-only match
    stack — no matter which process hosts the server. These gates default to
    the conservative/legacy values at import time, so any spawn path that skips
    this call serves a silently DEGRADED memory (the standalone ``persome mcp``
    stdio server ran BM25-only for exactly this reason). Every read-path
    entrance (daemon boot, MCP ``build_server`` — which covers stdio AND
    in-daemon HTTP) calls this; add it to any new one. Idempotent and cheap.
    """
    from ..writer import embeddings_client  # lazy: avoid an import cycle at module load
    from . import vectors as vectors_mod

    s = cfg.search
    dense_ready = bool(s.hybrid_enabled) and embeddings_client.available()
    vectors_mod.set_enabled(dense_ready)
    set_hybrid_config(enabled=dense_ready, recall_n=s.hybrid_recall_n, rrf_k=s.hybrid_rrf_k)
    set_pool_weights(slot=s.slot_pool_weight, relation=s.relation_pool_weight)
    set_relation_shadow(s.relation_include_shadow)
    set_contains_rerank(s.contains_pool_rerank)
    set_tags_matchable(s.tags_matchable)
    set_recency_decay(half_life_days=s.recency_half_life_days, floor=s.recency_decay_floor)
    if s.hybrid_enabled and not dense_ready:
        logger.info("hybrid retrieval on but no embeddings endpoint (OPENAI_*) — staying BM25-only")


def _dense_pool(
    conn: sqlite3.Connection,
    *,
    query: str,
    path_patterns: list[str] | None,
    top_k: int,
    embedder: Any | None,
    min_sim: float = 0.0,
) -> list[str]:
    """Top-``top_k`` LIVE entry ids by cosine similarity to the query embedding.

    ``min_sim`` is a STRICT floor (default 0.0 → sim must be positive): a zero/negative
    cosine is not a candidate — in production te3 space this is a no-op, but it keeps
    tiny-corpus evals honest (no zero-tier lottery seats in the RRF). The recognizer's
    standalone semantic layer passes a higher floor to drop sim≈0 noise entirely.

    Fail-open to ``[]`` (→ pure BM25) on any miss: dense disabled, embedding unavailable,
    no vectors yet, or a dim mismatch (stale vectors from a different model)."""
    from . import vectors as vectors_mod  # local import: avoid an import cycle at module load

    try:
        import numpy as np  # noqa: PLC0415

        embed = embedder
        if embed is None:
            from ..writer import embeddings_client  # noqa: PLC0415

            embed = embeddings_client.embed
        qv = embed(query)
        if qv is None:
            return []
        ids, mat = vectors_mod.live_matrix(conn, path_globs=path_patterns)
        if not ids or mat.size == 0:
            return []
        q = np.asarray(qv, dtype="<f4")
        if q.shape[0] != mat.shape[1]:
            return []  # dim mismatch — stale/foreign vectors; degrade to BM25
        qn = float(np.linalg.norm(q))
        if qn == 0.0:
            return []
        mn = np.linalg.norm(mat, axis=1)
        mn[mn == 0.0] = 1.0
        sims = (mat @ q) / (mn * qn)
        order = np.argsort(-sims)[:top_k]
        return [ids[i] for i in order if sims[i] > min_sim]
    except Exception:  # noqa: BLE001 — dense is additive; never break retrieval
        return []


def _rerank_by_query_sim(conn: sqlite3.Connection, ids: list[str], qv: Any) -> list[str]:
    """§7-10 池内查询感知重排: order pool candidates by cosine to the query
    embedding (desc, stable). The per-needle seat lands on the NEWEST entry,
    which is not necessarily the relevant one — the residual 3/12 relation
    probes had seats but exited top-k in the RRF tail. Candidates without a
    vector keep their relative recency order AFTER the ranked ones; any
    failure returns the input untouched (fail-open to recency)."""
    if not ids or qv is None:
        return ids
    try:
        import numpy as np  # noqa: PLC0415

        from . import vectors as vectors_mod  # noqa: PLC0415

        all_ids, mat = vectors_mod.live_matrix(conn, path_globs=None)
        if not all_ids or mat.size == 0:
            return ids
        q = np.asarray(qv, dtype="<f4")
        if q.shape[0] != mat.shape[1]:
            return ids
        qn = float(np.linalg.norm(q))
        if qn == 0.0:
            return ids
        pos = {eid: i for i, eid in enumerate(all_ids)}
        scored: list[tuple[float, int, str]] = []
        tail: list[str] = []
        for rank, eid in enumerate(ids):
            i = pos.get(eid)
            if i is None:
                tail.append(eid)
                continue
            v = mat[i]
            vn = float(np.linalg.norm(v))
            sim = float(v @ q) / (vn * qn) if vn else 0.0
            scored.append((-sim, rank, eid))
        scored.sort()
        sim_order = [eid for _s, _r, eid in scored] + tail
        # BLEND, don't replace (§7-10 per-probe verdict): pure sim order wins
        # the "seat landed on newest non-gold" cases but LOSES recency-intent
        # queries (最近/现在/状态… — three Persome probes flipped off). Both
        # signals are real, so fuse the two orderings with an in-pool RRF —
        # a candidate near the top of EITHER order stays near the pool head.
        return _rrf_fuse(ids, sim_order, rrf_k=5)
    except Exception:  # noqa: BLE001 — re-rank is decorative; recency is the floor
        return ids


def _rrf_fuse(*ranked_id_lists: list[str], rrf_k: int) -> list[str]:
    """Reciprocal Rank Fusion over ranked id lists → fused id order (best first).
    Each list is already ranked best-first; rank is 0-based; contribution 1/(k+rank+1)."""
    return _rrf_fuse_weighted([(p, 1.0) for p in ranked_id_lists], rrf_k=rrf_k)


def _rrf_fuse_weighted(pools: list[tuple[list[str], float]], *, rrf_k: int) -> list[str]:
    """Weighted RRF: contribution weight/(k+rank+1) per pool. A weight of 1.0 for
    every pool is byte-identical to classic RRF; down-weighting a pool makes it a
    BOOST (it can still introduce candidates and break ties) instead of an equal
    voter that can out-shout the text backbone."""
    scores: dict[str, float] = {}
    for ranked, weight in pools:
        if weight <= 0.0:
            continue
        for rank, eid in enumerate(ranked):
            scores[eid] = scores.get(eid, 0.0) + weight / (rrf_k + rank + 1)
    return sorted(scores, key=lambda e: scores[e], reverse=True)


def _cut_with_breadth(
    conn: sqlite3.Connection, ids: list[str], *, top_k: int, mmr_diversity: float
) -> list[str]:
    """Final candidate cut, honoring the consumer breadth knob (§3.4-3 / E1.1).

    ``mmr_diversity=0`` is a plain ``[:top_k]`` — byte-identical to pre-E1; a
    DR-style caller passes >0 to trade redundancy for coverage (deterministic
    widening via the existing MMR re-rank, never randomization)."""
    if mmr_diversity > 0.0:
        return _mmr_rerank(conn, ids[: max(top_k * 4, top_k)], top_k=top_k, diversity=mmr_diversity)
    return ids[:top_k]


def search_hybrid(
    conn: sqlite3.Connection,
    *,
    query: str,
    path_patterns: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    top_k: int = 5,
    include_superseded: bool = False,
    embedder: Any | None = None,
    mmr_diversity: float = 0.0,
) -> list[EntryHit]:
    """BM25 ∪ dense candidate pools fused by RRF. Drop-in for ``search``.

    Fail-open: when ``[search] hybrid_enabled`` is off, ``include_superseded`` is set
    (the dense index is live-only), or the dense pool comes back empty, this returns the
    exact BM25 ``search`` result. Retrieval counts are incremented only for the returned
    top-k (never the wider recall pool), matching ``search``'s side effect.
    ``mmr_diversity`` is the consumer breadth knob (E1.1) — it must keep working when a
    slot-less associative query degrades HERE, so the knob never silently dies."""
    if include_superseded:
        # archaeology mode — the caller explicitly wants history; no decay either.
        return search(
            conn,
            query=query,
            path_patterns=path_patterns,
            since=since,
            until=until,
            top_k=top_k,
            include_superseded=include_superseded,
        )
    recall_n = max(top_k, int(_HYBRID["recall_n"]))
    if not _HYBRID["enabled"]:
        # BM25-only install (no embeddings endpoint). Recency decay (#557) still
        # applies — over the wide candidate pool, so it can actually reorder —
        # and with decay+breadth off this is byte-identical to the legacy ``search``.
        if float(_RECENCY["half_life_days"]) <= 0.0 and mmr_diversity <= 0.0:
            return search(
                conn,
                query=query,
                path_patterns=path_patterns,
                since=since,
                until=until,
                top_k=top_k,
            )
        pool = _bm25_pool(
            conn,
            query=query,
            path_patterns=path_patterns,
            since=since,
            until=until,
            top_k=recall_n,
        )
        pool_by_id = {h.id: h for h in pool}
        ordered = _apply_recency(conn, [h.id for h in pool], until=until)
        ordered = _cut_with_breadth(conn, ordered, top_k=top_k, mmr_diversity=mmr_diversity)
        hits = [pool_by_id[eid] for eid in ordered]
        increment_retrieval_counts(conn, (h.id for h in hits))
        return hits
    bm25 = _bm25_pool(
        conn, query=query, path_patterns=path_patterns, since=since, until=until, top_k=recall_n
    )
    dense_ids = _dense_pool(
        conn, query=query, path_patterns=path_patterns, top_k=recall_n, embedder=embedder
    )
    if not dense_ids:
        ordered = _apply_recency(conn, [h.id for h in bm25], until=until)
        ordered = _cut_with_breadth(conn, ordered, top_k=top_k, mmr_diversity=mmr_diversity)
        by_id_bm = {h.id: h for h in bm25}
        hits = [by_id_bm[eid] for eid in ordered]
        increment_retrieval_counts(conn, (h.id for h in hits))
        return hits

    by_id = {h.id: h for h in bm25}
    fused_all = _rrf_fuse([h.id for h in bm25], dense_ids, rrf_k=int(_HYBRID["rrf_k"]))
    fused = _apply_recency(conn, fused_all, until=until)
    fused = _cut_with_breadth(conn, fused, top_k=top_k, mmr_diversity=mmr_diversity)
    # Dense-only ids have no EntryHit yet — fetch their rows honoring the same
    # superseded/since/until filters (a dense hit failing the filter is dropped).
    missing = [eid for eid in fused if eid not in by_id]
    if missing:
        clauses = ["id IN (" + ",".join("?" * len(missing)) + ")", "superseded = 0"]
        args: list[Any] = list(missing)
        if since:
            clauses.append("timestamp >= ?")
            args.append(since)
        if until:
            clauses.append("timestamp <= ?")
            args.append(until)
        sql = "SELECT id, path, timestamp, content FROM entries WHERE " + " AND ".join(clauses)
        for r in conn.execute(sql, args).fetchall():
            by_id[r["id"]] = EntryHit(
                id=r["id"], path=r["path"], timestamp=r["timestamp"], content=r["content"], rank=0.0
            )
    hits = [by_id[eid] for eid in fused if eid in by_id]
    increment_retrieval_counts(conn, (h.id for h in hits))
    return hits


def _contains_pool(
    conn: sqlite3.Connection,
    needles: list[str],
    *,
    top_k: int,
    since: str | None = None,
    until: str | None = None,
) -> list[str]:
    """Content-substring candidate pool, newest first — the WHO entity head and
    the WHERE scene head share this shape (memory-rebuild spec §3.3).

    Substring LIKE by design: unicode61 folds a Chinese run into one token, so
    FTS cannot find 张伟 inside 张伟负责… (nor 飞书 inside 在飞书上…) — exactly
    where these heads matter. A LIKE scan over live entries is the honest v1;
    a dedicated alias/scene FTS column is the optimization when production
    latency data asks for it. ``since``/``until`` prune the pool so the WHEN
    slot cuts across every head (§3.3 硬头先砍).
    """
    # §7-9 fair-share seating: fetch each needle's list (newest first), then
    # ROUND-ROBIN merge until top_k. Sequential fill starved every needle after
    # the first — with 44 relation neighbors, the alphabetically-first hub ate
    # all 50 seats and the probe targets got zero (2026-07-03 diagnosis, 5/12
    # relation probes → 9/12 after this fix). Needle order stays alphabetical:
    # strength-ordered needles were TRIED and measured WORSE (9/12 → 8/12 —
    # strong hubs push weak-edge targets' seats into the RRF rank tail), so
    # the neutral order is the data-verified choice, not an accident.
    per_needle: list[list[str]] = []
    for name in needles:
        needle = (name or "").strip()
        if not needle:
            continue
        clauses = ["superseded = 0", "content LIKE ?"]
        args: list[Any] = [f"%{needle}%"]
        if since:
            clauses.append("timestamp >= ?")
            args.append(since)
        if until:
            clauses.append("timestamp <= ?")
            args.append(until)
        args.append(top_k)
        rows = conn.execute(
            "SELECT id FROM entries WHERE "
            + " AND ".join(clauses)
            + " ORDER BY timestamp DESC LIMIT ?",
            args,
        ).fetchall()
        if rows:
            per_needle.append([r["id"] for r in rows])
    ids: list[str] = []
    seen: set[str] = set()
    depth = 0
    while len(ids) < top_k and any(depth < len(lst) for lst in per_needle):
        for lst in per_needle:
            if depth < len(lst):
                eid = lst[depth]
                if eid not in seen:
                    ids.append(eid)
                    seen.add(eid)
                    if len(ids) >= top_k:
                        break
        depth += 1
    return ids


def _window_pool(conn: sqlite3.Connection, *, since: str, until: str, top_k: int) -> list[str]:
    """WHEN-head candidate pool: live entries inside the distilled day window,
    newest first. The time slot is BOTH a filter (硬头先砍 — it prunes every
    other pool) and its own ranked list — without the list, a "6月5号做了什么"
    query with no lexical overlap would prune everything and return nothing.
    """
    rows = conn.execute(
        "SELECT id FROM entries WHERE superseded = 0 AND timestamp >= ? AND timestamp <= ? "
        "ORDER BY timestamp DESC LIMIT ?",
        (since, until, top_k),
    ).fetchall()
    return [r["id"] for r in rows]


def _bigram_sim(a: str, b: str) -> float:
    """Char-bigram Jaccard — the deterministic content-similarity the MMR
    re-rank uses (same shape as the sink's fuzzy fold; zero network)."""
    ga = {a[i : i + 2] for i in range(len(a) - 1)}
    gb = {b[i : i + 2] for i in range(len(b) - 1)}
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _mmr_rerank(
    conn: sqlite3.Connection, ids: list[str], *, top_k: int, diversity: float
) -> list[str]:
    """§3.4 step 3 — the CONSUMER knob for breadth (检索温度/MMR): iteratively
    pick the candidate maximizing rank_score − diversity·max_sim(selected).
    ``diversity=0`` is byte-identical to plain RRF order (the accuracy-first
    grounding default); a DR-style caller passes >0 to trade redundancy for
    coverage. Deterministic — the knob widens, it never randomizes."""
    if not ids:
        return []
    rows = conn.execute(
        "SELECT id, content FROM entries WHERE id IN (" + ",".join("?" * len(ids)) + ")",
        ids,
    ).fetchall()
    content = {r["id"]: r["content"] or "" for r in rows}
    rank_score = {eid: 1.0 / (i + 1) for i, eid in enumerate(ids)}
    selected: list[str] = []
    pool = [e for e in ids if e in content]
    while pool and len(selected) < top_k:
        best = max(
            pool,
            key=lambda e: (
                rank_score[e]
                - diversity
                * max((_bigram_sim(content[e], content[s]) for s in selected), default=0.0)
            ),
        )
        selected.append(best)
        pool.remove(best)
    return selected


def search_associative(
    conn: sqlite3.Connection,
    *,
    query: str,
    entities: list[str] | None = None,
    scene_terms: list[str] | None = None,
    path_patterns: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    top_k: int = 5,
    embedder: Any | None = None,
    early_exit: bool = True,
    mmr_diversity: float = 0.0,
    slot_pool_weight: float | None = None,
    relation_pool_weight: float | None = None,
    relation_include_shadow: bool | None = None,
    contains_rerank: bool | None = None,
) -> list[EntryHit]:
    """The associative read entrance (memory-rebuild spec §3.2/§3.3, incremental).

    ONE entrance, multi-head RRF over whatever slots the distilled Q occupies
    (``retrieval.associative.distill_q``): the text heads (BM25 lexical + dense
    semantic, exactly ``search_hybrid``'s pools), the WHO entity head, the
    WHERE scene head, the WHY/HOW relation head (graph expansion of the Q's
    identities over ACTIVE relation_edges — shadow stays out, §3.3 status
    gate), and — when the Q carries a day window — the WHEN window pool, with
    the window ALSO pruning every other pool (the time slot is both a hard
    filter and a ranked list). Absent slot = zero votes, no mode switch;
    all slots empty degrades to ``search_hybrid`` — one engine, one entrance.
    """
    entities = [e for e in (entities or []) if e and e.strip()]
    scene_terms = [s for s in (scene_terms or []) if s and s.strip()]
    has_window = bool(since and until)
    if not entities and not scene_terms and not has_window:
        return search_hybrid(
            conn,
            query=query,
            path_patterns=path_patterns,
            since=since,
            until=until,
            top_k=top_k,
            embedder=embedder,
            mmr_diversity=mmr_diversity,
        )
    recall_n = max(top_k, int(_HYBRID["recall_n"]))
    # §3.3/§3.4 step 1 — hard heads FIRST, so a unique high-confidence hard hit
    # can EARLY-EXIT before the expensive soft heads (the dense embedding call)
    # ever run. Adaptive computation: certainty buys latency.
    entity_ids = _contains_pool(conn, entities, top_k=recall_n, since=since, until=until)
    scene_ids = _contains_pool(conn, scene_terms, top_k=recall_n, since=since, until=until)
    window_ids = _window_pool(conn, since=since, until=until, top_k=recall_n) if has_window else []
    # WHY/HOW relation head (§3.3) is a HARD head too: graph expansion is a
    # zero-LLM, zero-embedding SQLite hop — classifying it as soft was a
    # cost-model error that made the entrance relationally BLIND whenever the
    # entity slot had a unique hit (e.g. the user's own entry): early exit
    # fired before the graph ever spread. Expand the Q's identities through
    # ACTIVE edges (shadow stays out — the status gate) and pool the entries
    # naming the REACHED identities: the query names 张伟, the graph knows
    # 张伟↔Bob, so Bob's entries join the vote without ever being mentioned.
    relation_ids: list[str] = []
    relation_shadow_ids: list[str] = []
    inc_shadow = (
        bool(_POOL_WEIGHTS.get("relation_shadow"))
        if relation_include_shadow is None
        else bool(relation_include_shadow)
    )
    if entities:
        from . import relation_edges as _edges_store

        try:
            reached = _edges_store.neighbors(conn, entities, depth=2, as_of=until)
        except Exception:  # noqa: BLE001 — the graph is an optional head, fail-open
            reached = set()
        neighbor_names = sorted(reached - set(entities) - {"self"})
        if neighbor_names:
            relation_ids = _contains_pool(
                conn, neighbor_names, top_k=recall_n, since=since, until=until
            )
        if inc_shadow:
            # §7-3 关系头喂食: audited-clean shadow edges may vote, at HALF the
            # relation weight — the shadow-ONLY reach (names not already reached
            # via ACTIVE) forms its own pool so 未证明 never outranks 已转正.
            try:
                reached_all = _edges_store.neighbors(
                    conn, entities, depth=2, as_of=until, include_shadow=True
                )
            except Exception:  # noqa: BLE001
                reached_all = set()
            shadow_names = sorted(reached_all - reached - set(entities) - {"self"})
            if shadow_names:
                relation_shadow_ids = _contains_pool(
                    conn, shadow_names, top_k=recall_n, since=since, until=until
                )
    hard_unique = {*entity_ids, *scene_ids, *window_ids, *relation_ids, *relation_shadow_ids}
    if early_exit and len(hard_unique) == 1:
        eid = next(iter(hard_unique))
        row = conn.execute(
            "SELECT id, path, timestamp, content FROM entries WHERE superseded = 0 AND id = ?",
            (eid,),
        ).fetchone()
        if row is not None:
            hit = EntryHit(
                id=row["id"],
                path=row["path"],
                timestamp=row["timestamp"],
                content=row["content"],
                rank=0.0,
            )
            increment_retrieval_counts(conn, (hit.id,))
            return [hit]
    # §7-10 池内查询感知重排 — AFTER the early exit (re-rank changes pool ORDER,
    # never membership, so the exit's uniqueness check is unaffected — and the
    # "unique hard hit → dense embedding never runs" latency invariant holds).
    do_rerank = (
        bool(_POOL_WEIGHTS.get("contains_rerank"))
        if contains_rerank is None
        else bool(contains_rerank)
    )
    if do_rerank and embedder is not None:
        try:
            _qv = embedder(query)
        except Exception:  # noqa: BLE001
            _qv = None
        if _qv is not None:
            entity_ids = _rerank_by_query_sim(conn, entity_ids, _qv)
            scene_ids = _rerank_by_query_sim(conn, scene_ids, _qv)
            relation_ids = _rerank_by_query_sim(conn, relation_ids, _qv)
            relation_shadow_ids = _rerank_by_query_sim(conn, relation_shadow_ids, _qv)
    bm25 = _bm25_pool(
        conn, query=query, path_patterns=path_patterns, since=since, until=until, top_k=recall_n
    )
    dense_ids = (
        _dense_pool(
            conn, query=query, path_patterns=path_patterns, top_k=recall_n, embedder=embedder
        )
        if _HYBRID["enabled"]
        else []
    )
    w_slot = _POOL_WEIGHTS["slot"] if slot_pool_weight is None else max(0.0, slot_pool_weight)
    w_rel = (
        _POOL_WEIGHTS["relation"]
        if relation_pool_weight is None
        else max(0.0, relation_pool_weight)
    )
    pools = [
        (p, w)
        for p, w in (
            ([h.id for h in bm25], 1.0),
            (dense_ids, 1.0),
            (entity_ids, w_slot),
            (scene_ids, w_slot),
            (window_ids, w_slot),
            (relation_ids, w_rel),
            (relation_shadow_ids, w_rel * 0.5),
        )
        if p and w > 0.0
    ]
    if not pools:
        return []
    fused_all = _rrf_fuse_weighted(pools, rrf_k=int(_HYBRID["rrf_k"]))
    # 轴B (#557): recency decay re-ranks the fused candidates before the cut —
    # anchored at the Q's own ``until`` when a day window was distilled/passed.
    fused_all = _apply_recency(conn, fused_all, until=until)
    if mmr_diversity > 0.0:
        fused = _mmr_rerank(
            conn, fused_all[: max(top_k * 4, top_k)], top_k=top_k, diversity=mmr_diversity
        )
    else:
        fused = fused_all[:top_k]
    by_id = {h.id: h for h in bm25}
    missing = [eid for eid in fused if eid not in by_id]
    if missing:
        clauses = ["superseded = 0", "id IN (" + ",".join("?" * len(missing)) + ")"]
        args: list[Any] = list(missing)
        if since:
            clauses.append("timestamp >= ?")
            args.append(since)
        if until:
            clauses.append("timestamp <= ?")
            args.append(until)
        sql = "SELECT id, path, timestamp, content FROM entries WHERE " + " AND ".join(clauses)
        for r in conn.execute(sql, args).fetchall():
            by_id[r["id"]] = EntryHit(
                id=r["id"], path=r["path"], timestamp=r["timestamp"], content=r["content"], rank=0.0
            )
    hits = [by_id[eid] for eid in fused if eid in by_id]
    increment_retrieval_counts(conn, (h.id for h in hits))
    return hits


# ─── captures (FTS5) ───────────────────────────────────────────────────────


@dataclass
class CaptureHit:
    """A captures-table row paired with its FTS rank + snippet."""

    id: str  # capture file stem
    timestamp: str
    app_name: str
    bundle_id: str
    window_title: str
    focused_role: str
    focused_value: str
    url: str
    snippet: str  # FTS5 snippet() with the matched tokens highlighted
    rank: float  # bm25 score (lower = better); 0.0 for non-search recent()


def insert_capture(
    conn: sqlite3.Connection,
    *,
    id: str,
    timestamp: str,
    app_name: str,
    bundle_id: str,
    window_title: str,
    focused_role: str,
    focused_value: str,
    visible_text: str,
    url: str,
) -> None:
    """Upsert one capture row. Triggers keep captures_fts in sync."""
    conn.execute(
        """
        INSERT INTO captures
            (id, timestamp, app_name, bundle_id, window_title,
             focused_role, focused_value, visible_text, url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            timestamp=excluded.timestamp,
            app_name=excluded.app_name,
            bundle_id=excluded.bundle_id,
            window_title=excluded.window_title,
            focused_role=excluded.focused_role,
            focused_value=excluded.focused_value,
            visible_text=excluded.visible_text,
            url=excluded.url
        """,
        (
            id,
            timestamp,
            app_name,
            bundle_id,
            window_title,
            focused_role,
            focused_value,
            visible_text,
            url,
        ),
    )


def delete_capture(conn: sqlite3.Connection, capture_id: str) -> None:
    conn.execute("DELETE FROM captures WHERE id=?", (capture_id,))


def search_captures(
    conn: sqlite3.Connection,
    *,
    query: str,
    since: str | None = None,
    until: str | None = None,
    app_name: str | None = None,
    limit: int = 10,
) -> list[CaptureHit]:
    """BM25 + snippet search over capture S1 fields.

    The ``app_name`` filter is a case-insensitive substring match on the
    ``captures.app_name`` column (not via FTS), so callers can filter by
    "Cursor" without competing for FTS slots.
    """
    safe_query = _safe_fts_query(query)
    if not safe_query or safe_query == '""':
        return []
    clauses = ["captures_fts MATCH ?"]
    args: list[Any] = [safe_query]
    if since:
        clauses.append("c.timestamp >= ?")
        args.append(since)
    if until:
        clauses.append("c.timestamp <= ?")
        args.append(until)
    if app_name:
        clauses.append("LOWER(c.app_name) LIKE ?")
        args.append(f"%{app_name.lower()}%")
    sql = (
        "SELECT c.id, c.timestamp, c.app_name, c.bundle_id, c.window_title, "
        "       c.focused_role, c.focused_value, c.url, "
        "       snippet(captures_fts, -1, '[', ']', '…', 16) AS snippet, "
        "       bm25(captures_fts) AS rank "
        "  FROM captures c "
        "  JOIN captures_fts ON captures_fts.rowid = c.rowid "
        " WHERE " + " AND ".join(clauses) + " ORDER BY rank LIMIT ?"
    )
    args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    return [
        CaptureHit(
            id=r["id"],
            timestamp=r["timestamp"],
            app_name=r["app_name"] or "",
            bundle_id=r["bundle_id"] or "",
            window_title=r["window_title"] or "",
            focused_role=r["focused_role"] or "",
            focused_value=r["focused_value"] or "",
            url=r["url"] or "",
            snippet=r["snippet"] or "",
            rank=r["rank"],
        )
        for r in rows
    ]


def recent_captures(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    until: str | None = None,
    app_name: str | None = None,
    limit: int = 20,
) -> list[CaptureHit]:
    """Newest-first capture rows without keyword filtering — used by current_context."""
    clauses: list[str] = []
    args: list[Any] = []
    if since:
        clauses.append("timestamp >= ?")
        args.append(since)
    if until:
        clauses.append("timestamp <= ?")
        args.append(until)
    if app_name:
        clauses.append("LOWER(app_name) LIKE ?")
        args.append(f"%{app_name.lower()}%")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        "SELECT id, timestamp, app_name, bundle_id, window_title, "
        "       focused_role, focused_value, url "
        f"  FROM captures {where} "
        " ORDER BY timestamp DESC LIMIT ?"
    )
    args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    return [
        CaptureHit(
            id=r["id"],
            timestamp=r["timestamp"],
            app_name=r["app_name"] or "",
            bundle_id=r["bundle_id"] or "",
            window_title=r["window_title"] or "",
            focused_role=r["focused_role"] or "",
            focused_value=r["focused_value"] or "",
            url=r["url"] or "",
            snippet="",
            rank=0.0,
        )
        for r in rows
    ]


def get_capture_visible_text(conn: sqlite3.Connection, capture_id: str) -> str:
    """Read just the visible_text field for a capture. Used by current_context."""
    r = conn.execute("SELECT visible_text FROM captures WHERE id=?", (capture_id,)).fetchone()
    return (r["visible_text"] if r else "") or ""


# ─── on-device OCR text ─────────────────────────────────────────────────────
# OCR is local + synchronous (capture/ocr_local.py): the result is backfilled
# straight into captures.visible_text. The capture-buffer JSON on disk keeps its
# empty visible_text, so consumers that read the JSON (timeline aggregator, MCP
# read_recent_capture) recover the OCR text from the captures row via the reader
# below. (Formerly this read ocr_jobs.result_text; that async table is retired.)


def get_ocr_result_for_capture(conn: sqlite3.Connection, capture_id: str) -> str | None:
    """Return the OCR-backfilled visible_text for a capture, or None if absent/empty."""
    r = conn.execute(
        "SELECT visible_text FROM captures WHERE id=?",
        (capture_id,),
    ).fetchone()
    return (r["visible_text"] if r else None) or None


def backfill_capture_ocr_text(conn: sqlite3.Connection, capture_id: str, text: str) -> None:
    """Update captures.visible_text when OCR completes (only if currently empty).

    The captures_au trigger keeps captures_fts in sync automatically.
    """
    conn.execute(
        "UPDATE captures SET visible_text = ? WHERE id = ? AND (visible_text IS NULL OR visible_text = '')",
        (text, capture_id),
    )


# ─── memory entries (FTS5) — read paths ────────────────────────────────────


def recent(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    limit: int = 20,
    prefix_filter: list[str] | None = None,
    include_superseded: bool = False,
) -> list[EntryHit]:
    clauses: list[str] = []
    args: list[Any] = []
    if since:
        clauses.append("timestamp >= ?")
        args.append(since)
    if prefix_filter:
        placeholders = ",".join("?" * len(prefix_filter))
        clauses.append(f"prefix IN ({placeholders})")
        args.extend(prefix_filter)
    if not include_superseded:
        clauses.append("superseded = 0")

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        f"SELECT id, path, timestamp, content, 0.0 AS rank FROM entries {where} "
        "ORDER BY timestamp DESC LIMIT ?"
    )
    args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    return [
        EntryHit(
            id=r["id"], path=r["path"], timestamp=r["timestamp"], content=r["content"], rank=0.0
        )
        for r in rows
    ]
