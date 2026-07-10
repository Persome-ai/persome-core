"""Drain the dense-retrieval embed queue: pending entries → te3-large vectors (via relay).

Queue worker for optional hybrid-retrieval embeddings.

The write path only ENQUEUES (``vectors.maybe_enqueue``); this is where the actual embedding
happens — off the capture path, on a daemon tick, in batches, fail-open. A failed batch leaves
its entries queued (retried next tick); those entries stay BM25-only meanwhile. Idempotent and
bounded (``embed_tick_max`` per tick), so it never starves capture or runs away on a big backlog.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .logger import get
from .store import fts
from .store import vectors as vectors_mod

_log = get("persome.vectors_tick")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def run_embed_once(
    cfg: Any,
    *,
    embedder: Callable[[list[str]], list[list[float] | None]] | None = None,
    now: str | None = None,
) -> tuple[int, int]:
    """Embed up to ``cfg.search.embed_tick_max`` pending entries in batches. Returns
    (embedded_this_tick, queued_remaining). ``embedder`` defaults to the relay client; tests
    inject a fake. No-op (0, 0) when hybrid is disabled."""
    sc = cfg.search
    if not getattr(sc, "hybrid_enabled", False):
        return 0, 0
    if embedder is None:
        from .writer import embeddings_client

        embedder = lambda texts: embeddings_client.embed_batch(texts, model=sc.embed_model)  # noqa: E731
    ts = now or _now_iso()
    embedded = 0
    with fts.cursor() as conn:
        remaining_budget = int(getattr(sc, "embed_tick_max", 512))
        batch_size = max(1, int(getattr(sc, "embed_batch_size", 64)))
        while remaining_budget > 0:
            batch = vectors_mod.pending_batch(conn, limit=min(batch_size, remaining_budget))
            if not batch:
                break
            ids = [b[0] for b in batch]
            texts = [b[1] for b in batch]
            vecs = embedder(texts)
            written = vectors_mod.save_vectors(
                conn, list(zip(ids, vecs, strict=True)), model=sc.embed_model, embedded_at=ts
            )
            embedded += written
            remaining_budget -= len(batch)
            # all-fail batch (relay down) → stop this tick rather than spin; retry next tick
            if written == 0:
                _log.warning(
                    "vector-embed-tick: batch of %d all failed — stopping tick", len(batch)
                )
                break
        vectors_mod.gc_orphans(conn)
        _, queued = vectors_mod.count(conn)
    return embedded, queued


def backfill(cfg: Any, *, limit: int | None = None) -> int:
    """Enqueue every LIVE entry that has no vector yet (rebuild / one-shot backfill). Returns
    the number enqueued. The embed-tick then drains it. Enqueues REGARDLESS of the runtime
    enabled flag (an explicit backfill is the operator opting in)."""
    enqueued = 0
    with fts.cursor() as conn:
        rows = conn.execute(
            """
            SELECT e.id FROM entries e
            WHERE e.superseded = 0
              AND e.id NOT IN (SELECT entry_id FROM entry_vectors)
              AND e.id NOT IN (SELECT entry_id FROM vector_queue)
            """
            + (" LIMIT ?" if limit else ""),
            ([limit] if limit else []),
        ).fetchall()
        ts = _now_iso()
        for r in rows:
            vectors_mod.enqueue(conn, r["id"], ts=ts)
            enqueued += 1
    return enqueued
