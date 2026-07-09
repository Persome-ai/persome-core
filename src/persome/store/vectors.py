"""Dense-retrieval vector index for memory entries.

Spec: docs/superpowers/specs/2026-06-25-production-hybrid-retrieval-design.md (Phase 1).

A new ``entry_vectors`` table holds one embedding per memory entry (text-embedding-3-large,
3072-d float32 BLOB). Writes do NOT embed inline (that would block capture on a network call):
``derived_append_rows`` only ENQUEUES the entry into ``vector_queue``; a daemon tick drains the
queue, embeds in batches via the relay, and upserts the vectors. Dense search filters to LIVE
(``entries.superseded=0``) entries by joining ``entries``; a periodic GC drops orphan vectors.

All of this is gated by ``cfg.search.hybrid_enabled`` (default off): when off, nothing is
enqueued and the table stays empty — byte-identical to the BM25-only status quo.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import numpy as np

_MODEL_DEFAULT = "text-embedding-3-large"

# The write choke point (derived_append_rows) has no cfg in scope, so the daemon sets this
# at boot from cfg.search.hybrid_enabled. Default False → no enqueue (the queue never grows
# when hybrid is off; byte-identical to the BM25-only status quo). Backfill paths enqueue
# explicitly regardless of this flag.
_ENABLED = False


def set_enabled(value: bool) -> None:
    global _ENABLED
    _ENABLED = bool(value)


def is_enabled() -> bool:
    return _ENABLED


def maybe_enqueue(conn: sqlite3.Connection, entry_id: str, *, ts: str) -> None:
    """Enqueue ONLY when hybrid is enabled — the write-path gate (cheap no-op when off)."""
    if _ENABLED:
        enqueue(conn, entry_id, ts=ts)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the vector table + pending queue (idempotent; called from fts.connect)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS entry_vectors (
            entry_id    TEXT PRIMARY KEY,
            dim         INTEGER NOT NULL,
            model       TEXT NOT NULL,
            vector      BLOB NOT NULL,
            embedded_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS vector_queue (
            entry_id    TEXT PRIMARY KEY,
            enqueued_at TEXT NOT NULL
        );
        """
    )


# ── pack / unpack ────────────────────────────────────────────────────────────
def pack(vec: list[float] | np.ndarray) -> bytes:
    """float32 little-endian bytes for a vector."""
    arr = np.asarray(vec, dtype="<f4")
    return arr.tobytes()


def unpack(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype="<f4")


# ── write-path enqueue ───────────────────────────────────────────────────────
def enqueue(conn: sqlite3.Connection, entry_id: str, *, ts: str) -> None:
    """Mark an entry as needing an embedding (cheap DB insert, no network). The
    write choke point calls this AFTER fts.insert_entry when hybrid is enabled."""
    conn.execute(
        "INSERT OR IGNORE INTO vector_queue(entry_id, enqueued_at) VALUES (?, ?)",
        (entry_id, ts),
    )


def pending_batch(conn: sqlite3.Connection, *, limit: int) -> list[tuple[str, str]]:
    """Up to ``limit`` (entry_id, content) pairs awaiting embedding, LIVE entries only.
    A queued entry whose row was superseded/deleted is skipped here and GC'd separately."""
    rows = conn.execute(
        """
        SELECT q.entry_id, e.content
        FROM vector_queue q JOIN entries e ON e.id = q.entry_id
        WHERE e.superseded = 0
        ORDER BY q.enqueued_at
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [(r["entry_id"], r["content"]) for r in rows]


def save_vectors(
    conn: sqlite3.Connection,
    items: list[tuple[str, list[float] | np.ndarray | None]],
    *,
    model: str = _MODEL_DEFAULT,
    embedded_at: str,
) -> int:
    """Upsert vectors and clear them from the queue. ``None`` vector = embed failed → leave
    it queued (no upsert) so the next tick retries; the entry stays BM25-only meanwhile.
    Returns the number of vectors actually written."""
    written = 0
    for entry_id, vec in items:
        if vec is None:
            continue
        blob = pack(vec)
        dim = len(blob) // 4
        conn.execute(
            """
            INSERT INTO entry_vectors(entry_id, dim, model, vector, embedded_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(entry_id) DO UPDATE SET
                dim=excluded.dim, model=excluded.model,
                vector=excluded.vector, embedded_at=excluded.embedded_at
            """,
            (entry_id, dim, model, blob, embedded_at),
        )
        conn.execute("DELETE FROM vector_queue WHERE entry_id = ?", (entry_id,))
        written += 1
    return written


def gc_orphans(conn: sqlite3.Connection) -> int:
    """Drop vectors + queue rows whose entry is gone or superseded (vectors for dead
    entries are never returned by search; this just bounds the table). Returns rows removed."""
    cur = conn.execute(
        "DELETE FROM entry_vectors WHERE entry_id NOT IN (SELECT id FROM entries WHERE superseded = 0)"
    )
    removed = cur.rowcount or 0
    conn.execute(
        "DELETE FROM vector_queue WHERE entry_id NOT IN (SELECT id FROM entries WHERE superseded = 0)"
    )
    return removed


def evict(conn: sqlite3.Connection, entry_id: str) -> None:
    """Best-effort drop of one entry's vector + queue row (supersede/retire path)."""
    conn.execute("DELETE FROM entry_vectors WHERE entry_id = ?", (entry_id,))
    conn.execute("DELETE FROM vector_queue WHERE entry_id = ?", (entry_id,))


# ── read-path: live vectors for dense search (cached) ────────────────────────
# Brute-force cosine needs the WHOLE live vector matrix; rebuilding it (JOIN + per-row
# np.frombuffer + vstack) on EVERY hybrid query is O(N·D) and dominates search as the corpus
# grows. So the assembled matrix is cached in-process and reused until the vectors change.
# The cache key is a cheap validity token — (db file, vector count, max embedded_at) — so ANY
# write that touches entry_vectors (embed tick upsert, supersede/retire evict, gc) bumps the
# token and forces a rebuild, with no coupling to the write sites. A supersede that only flips
# entries.superseded (vector kept) is handled at materialization (search_hybrid re-fetches hit
# rows live), so a slightly-stale matrix can never surface a dead entry — only miss a vector for
# at most one refresh. Bounded by ``limit``; one matrix per (scope, limit) sharing one token.
_MATRIX_CACHE: dict[tuple, tuple[list[str], Any]] = {}
_MATRIX_TOKEN: str | None = None


def _validity_token(conn: sqlite3.Connection) -> str:
    dbfile = ""
    for r in conn.execute("PRAGMA database_list"):
        if r["name"] == "main":
            dbfile = r["file"] or ""
            break
    row = conn.execute("SELECT COUNT(*), COALESCE(MAX(embedded_at), '') FROM entry_vectors").fetchone()
    return f"{dbfile}|{int(row[0])}|{row[1]}"


def clear_matrix_cache() -> None:
    """Drop the cached dense matrix (tests / explicit invalidation)."""
    global _MATRIX_TOKEN
    _MATRIX_CACHE.clear()
    _MATRIX_TOKEN = None


def live_matrix(
    conn: sqlite3.Connection,
    *,
    path_globs: list[str] | None = None,
    limit: int = 50000,
) -> tuple[list[str], Any]:
    """Return (entry_ids, MxD float32 matrix) of LIVE entries' vectors, optionally scoped by
    path GLOB(s). The caller cosine-ranks the query against the matrix. Cached in-process and
    reused until the vectors change (see the cache note above); bounded by ``limit`` (most-recent
    first) so a huge corpus can't blow up a single dense pass."""
    global _MATRIX_TOKEN
    token = _validity_token(conn)
    if token != _MATRIX_TOKEN:
        _MATRIX_CACHE.clear()
        _MATRIX_TOKEN = token
    key = (tuple(path_globs) if path_globs else (), limit)
    hit = _MATRIX_CACHE.get(key)
    if hit is not None:
        return hit

    clauses = ["e.superseded = 0"]
    params: list[Any] = []
    if path_globs:
        clauses.append("(" + " OR ".join("e.path GLOB ?" for _ in path_globs) + ")")
        params.extend(path_globs)
    sql = (
        "SELECT v.entry_id, v.vector FROM entry_vectors v JOIN entries e ON e.id = v.entry_id "
        "WHERE " + " AND ".join(clauses) + " ORDER BY v.embedded_at DESC LIMIT ?"
    )
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        result: tuple[list[str], Any] = ([], np.empty((0, 0), dtype="<f4"))
    else:
        ids = [r["entry_id"] for r in rows]
        mat = np.vstack([unpack(r["vector"]) for r in rows])
        result = (ids, mat)
    _MATRIX_CACHE[key] = result
    return result


def count(conn: sqlite3.Connection) -> tuple[int, int]:
    """(embedded vectors, queued pending) — for status / tests."""
    v = conn.execute("SELECT COUNT(*) FROM entry_vectors").fetchone()[0]
    q = conn.execute("SELECT COUNT(*) FROM vector_queue").fetchone()[0]
    return int(v), int(q)
