"""Phase 1 of the production hybrid-retrieval spec: the dense-vector index + embed tick.

Contract: vector storage stays deterministic and degrades cleanly without a provider.

Offline + deterministic (no network, no LLM): the relay embedder is replaced by a fake.
Covers the vector DAO (schema/enqueue/save/evict/gc/live_matrix/count + the set_enabled
write-path gate), the relay client's fail-open contract, and run_embed_once / backfill.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from persome import config as config_mod
from persome import vectors_tick
from persome.store import fts
from persome.store import vectors as vectors_mod
from persome.writer import embeddings_client


@pytest.fixture(autouse=True)
def _reset_enabled():
    """The write/read gates are module globals; keep them off unless a test opts in."""
    vectors_mod.set_enabled(False)
    fts.set_hybrid_config(enabled=False, recall_n=50, rrf_k=20)
    vectors_mod.clear_matrix_cache()
    yield
    vectors_mod.set_enabled(False)
    fts.set_hybrid_config(enabled=False, recall_n=50, rrf_k=20)
    vectors_mod.clear_matrix_cache()


def _seed_entry(conn, entry_id: str, *, content: str = "hello", superseded: int = 0) -> None:
    fts.insert_entry(
        conn,
        id=entry_id,
        path="topic-x.md",
        prefix="topic",
        timestamp="2026-06-25T10:00:00+08:00",
        tags="",
        content=content,
        superseded=superseded,
    )


def _hybrid_cfg(**search_overrides):
    cfg = config_mod.load()
    return replace(cfg, search=replace(cfg.search, hybrid_enabled=True, **search_overrides))


# ── pack / unpack ────────────────────────────────────────────────────────────
def test_pack_unpack_roundtrip(ac_root):
    vec = [0.1, -0.2, 0.3, 0.4]
    out = vectors_mod.unpack(vectors_mod.pack(vec))
    assert out.dtype == np.dtype("<f4")
    assert np.allclose(out, vec, atol=1e-6)


# ── write-path gate ──────────────────────────────────────────────────────────
def test_maybe_enqueue_respects_enabled_flag(ac_root):
    with fts.cursor() as conn:
        _seed_entry(conn, "e1")
        vectors_mod.maybe_enqueue(conn, "e1", ts="t")  # disabled → no-op
        assert vectors_mod.count(conn) == (0, 0)
        vectors_mod.set_enabled(True)
        vectors_mod.maybe_enqueue(conn, "e1", ts="t")  # enabled → enqueued
        assert vectors_mod.count(conn) == (0, 1)


def test_enqueue_is_idempotent(ac_root):
    with fts.cursor() as conn:
        _seed_entry(conn, "e1")
        vectors_mod.enqueue(conn, "e1", ts="t")
        vectors_mod.enqueue(conn, "e1", ts="t2")  # INSERT OR IGNORE
        assert vectors_mod.count(conn) == (0, 1)


# ── pending_batch / save / evict / gc ────────────────────────────────────────
def test_pending_batch_live_only_and_save(ac_root):
    with fts.cursor() as conn:
        _seed_entry(conn, "live", content="a")
        _seed_entry(conn, "dead", content="b", superseded=1)
        vectors_mod.enqueue(conn, "live", ts="t")
        vectors_mod.enqueue(conn, "dead", ts="t")  # queued but superseded
        batch = vectors_mod.pending_batch(conn, limit=10)
        assert batch == [("live", "a")]  # dead excluded by the JOIN

        written = vectors_mod.save_vectors(conn, [("live", [1.0, 2.0, 3.0])], embedded_at="t")
        assert written == 1
        embedded, queued = vectors_mod.count(conn)
        assert embedded == 1
        assert queued == 1  # only "live" dequeued; "dead" still queued (GC's job)


def test_save_vectors_none_leaves_queued(ac_root):
    with fts.cursor() as conn:
        _seed_entry(conn, "e1")
        vectors_mod.enqueue(conn, "e1", ts="t")
        written = vectors_mod.save_vectors(conn, [("e1", None)], embedded_at="t")
        assert written == 0
        assert vectors_mod.count(conn) == (0, 1)  # still queued for next tick


def test_save_vectors_upsert(ac_root):
    with fts.cursor() as conn:
        _seed_entry(conn, "e1")
        vectors_mod.save_vectors(conn, [("e1", [1.0, 1.0])], embedded_at="t1")
        vectors_mod.save_vectors(conn, [("e1", [9.0, 9.0])], embedded_at="t2")
        ids, mat = vectors_mod.live_matrix(conn)
        assert ids == ["e1"]
        assert np.allclose(mat[0], [9.0, 9.0])  # latest write wins


def test_evict_drops_vector_and_queue(ac_root):
    with fts.cursor() as conn:
        _seed_entry(conn, "e1")
        vectors_mod.enqueue(conn, "e1", ts="t")
        vectors_mod.save_vectors(conn, [("e1", [1.0])], embedded_at="t")
        vectors_mod.enqueue(conn, "e1", ts="t")  # re-queued
        vectors_mod.evict(conn, "e1")
        assert vectors_mod.count(conn) == (0, 0)


def test_gc_orphans(ac_root):
    with fts.cursor() as conn:
        _seed_entry(conn, "live")
        _seed_entry(conn, "dead", superseded=1)
        vectors_mod.save_vectors(
            conn, [("live", [1.0]), ("dead", [2.0]), ("ghost", [3.0])], embedded_at="t"
        )
        # "ghost" has no entries row at all; "dead" is superseded
        removed = vectors_mod.gc_orphans(conn)
        assert removed == 2
        ids, _ = vectors_mod.live_matrix(conn)
        assert ids == ["live"]


def test_live_matrix_cache_hits_and_invalidates(ac_root):
    vectors_mod.clear_matrix_cache()
    with fts.cursor() as conn:
        _seed_entry(conn, "a", content="x")
        vectors_mod.save_vectors(conn, [("a", [1.0, 0.0])], embedded_at="t1")
        ids1, mat1 = vectors_mod.live_matrix(conn)
        ids2, mat2 = vectors_mod.live_matrix(conn)
        assert mat1 is mat2  # second call is a cache HIT (same object, no rebuild)
        # a new vector changes the validity token → rebuild, fresh object
        _seed_entry(conn, "b", content="y")
        vectors_mod.save_vectors(conn, [("b", [0.0, 1.0])], embedded_at="t2")
        ids3, mat3 = vectors_mod.live_matrix(conn)
        assert mat3 is not mat1
        assert set(ids3) == {"a", "b"}
    vectors_mod.clear_matrix_cache()


def test_live_matrix_path_scoped(ac_root):
    with fts.cursor() as conn:
        _seed_entry(conn, "t1")  # topic-x.md
        fts.insert_entry(
            conn,
            id="p1",
            path="person-bob.md",
            prefix="person",
            timestamp="2026-06-25T10:00:00+08:00",
            tags="",
            content="bob",
        )
        vectors_mod.save_vectors(conn, [("t1", [1.0]), ("p1", [2.0])], embedded_at="t")
        ids, _ = vectors_mod.live_matrix(conn, path_globs=["person-*.md"])
        assert ids == ["p1"]


# ── embeddings_client fail-open ──────────────────────────────────────────────
def test_embed_batch_unconfigured_returns_none(ac_root, monkeypatch):
    monkeypatch.setattr(embeddings_client, "provider_api_key", lambda _p: None)
    monkeypatch.setattr(embeddings_client, "provider_base_url", lambda _p: None)
    assert embeddings_client.available() is False
    assert embeddings_client.embed_batch(["a", "b"]) == [None, None]
    assert embeddings_client.embed_batch([]) == []


def test_embed_batch_bad_shape_returns_none(ac_root, monkeypatch):
    monkeypatch.setattr(embeddings_client, "provider_api_key", lambda _p: "jwt")
    monkeypatch.setattr(embeddings_client, "provider_base_url", lambda _p: "https://relay/api/llm")

    class _Resp:
        status_code = 200

        def json(self):
            return {"data": [{"index": 0, "embedding": [1.0]}]}  # 1 item for 2 inputs

    class _Client:
        def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(embeddings_client, "_http_client", lambda: _Client())
    # shape mismatch → retries exhausted → all None (fail-open, never raises)
    assert embeddings_client.embed_batch(["a", "b"]) == [None, None]


def test_embed_batch_happy_path_orders_by_index(ac_root, monkeypatch):
    monkeypatch.setattr(embeddings_client, "provider_api_key", lambda _p: "jwt")
    monkeypatch.setattr(embeddings_client, "provider_base_url", lambda _p: "https://relay/api/llm")

    class _Resp:
        status_code = 200

        def json(self):
            # deliberately out of order — client must sort by "index"
            return {"data": [{"index": 1, "embedding": [2.0]}, {"index": 0, "embedding": [1.0]}]}

    class _Client:
        def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(embeddings_client, "_http_client", lambda: _Client())
    assert embeddings_client.embed_batch(["a", "b"]) == [[1.0], [2.0]]


# ── run_embed_once / backfill ────────────────────────────────────────────────
def test_run_embed_once_noop_when_disabled(ac_root):
    cfg = config_mod.load()
    cfg = replace(cfg, search=replace(cfg.search, hybrid_enabled=False))  # explicitly off
    assert vectors_tick.run_embed_once(cfg, embedder=lambda t: [[1.0]] * len(t)) == (0, 0)


def test_run_embed_once_drains_queue(ac_root):
    vectors_mod.set_enabled(True)
    with fts.cursor() as conn:
        for i in range(5):
            _seed_entry(conn, f"e{i}", content=f"c{i}")
            vectors_mod.enqueue(conn, f"e{i}", ts="t")

    calls: list[int] = []

    def fake_embedder(texts):
        calls.append(len(texts))
        return [[float(len(t))] for t in texts]

    cfg = _hybrid_cfg(embed_batch_size=2, embed_tick_max=512)
    embedded, queued = vectors_tick.run_embed_once(cfg, embedder=fake_embedder)
    assert embedded == 5
    assert queued == 0
    assert calls == [2, 2, 1]  # batched by embed_batch_size
    with fts.cursor() as conn:
        ids, mat = vectors_mod.live_matrix(conn)
        assert sorted(ids) == ["e0", "e1", "e2", "e3", "e4"]
        assert mat.shape == (5, 1)


def test_run_embed_once_all_fail_stops_and_requeues(ac_root):
    vectors_mod.set_enabled(True)
    with fts.cursor() as conn:
        _seed_entry(conn, "e1")
        vectors_mod.enqueue(conn, "e1", ts="t")

    cfg = _hybrid_cfg(embed_batch_size=8)
    embedded, queued = vectors_tick.run_embed_once(cfg, embedder=lambda t: [None] * len(t))
    assert embedded == 0
    assert queued == 1  # left queued for the next tick — stays BM25-only meanwhile


def test_run_embed_once_respects_tick_max(ac_root):
    vectors_mod.set_enabled(True)
    with fts.cursor() as conn:
        for i in range(10):
            _seed_entry(conn, f"e{i}")
            vectors_mod.enqueue(conn, f"e{i}", ts="t")

    cfg = _hybrid_cfg(embed_batch_size=4, embed_tick_max=4)
    embedded, queued = vectors_tick.run_embed_once(cfg, embedder=lambda t: [[1.0]] * len(t))
    assert embedded == 4  # bounded by embed_tick_max
    assert queued == 6


def test_backfill_enqueues_live_unembedded(ac_root):
    with fts.cursor() as conn:
        _seed_entry(conn, "live")
        _seed_entry(conn, "dead", superseded=1)
        _seed_entry(conn, "already")
        vectors_mod.save_vectors(conn, [("already", [1.0])], embedded_at="t")

    cfg = config_mod.load()  # backfill enqueues regardless of the flag
    enqueued = vectors_tick.backfill(cfg)
    assert enqueued == 1  # only "live" (dead=superseded, already=has vector)
    with fts.cursor() as conn:
        assert vectors_mod.pending_batch(conn, limit=10) == [("live", "hello")]


# ── search_hybrid (Phase 2: BM25 ⊕ dense → RRF, fail-open BM25) ───────────────
def _doc(conn, eid: str, content: str, vec=None) -> None:
    _seed_entry(conn, eid, content=content)
    if vec is not None:
        vectors_mod.save_vectors(conn, [(eid, vec)], embedded_at="t")


def test_search_hybrid_disabled_is_byte_identical_to_bm25(ac_root):
    with fts.cursor() as conn:
        _doc(conn, "a", "apple pie recipe")
        _doc(conn, "b", "banana bread")
        # disabled (default) → must equal the pure BM25 search
        h = fts.search_hybrid(conn, query="apple", top_k=5)
        s = fts.search(conn, query="apple", top_k=5)
        assert [x.id for x in h] == [x.id for x in s] == ["a"]


def test_search_hybrid_include_superseded_delegates_to_bm25(ac_root):
    fts.set_hybrid_config(enabled=True, recall_n=50, rrf_k=20)
    with fts.cursor() as conn:
        _doc(conn, "old", "apple turnover", vec=[1.0, 0.0])
        conn.execute("UPDATE entries SET superseded=1 WHERE id='old'")
        # include_superseded → dense index is live-only, so it must use BM25 (finds the dead row)
        hits = fts.search_hybrid(
            conn, query="apple", top_k=5, include_superseded=True, embedder=lambda _q: [1.0, 0.0]
        )
        assert [h.id for h in hits] == ["old"]


def test_search_hybrid_no_vectors_falls_back_to_bm25(ac_root):
    fts.set_hybrid_config(enabled=True, recall_n=50, rrf_k=20)
    with fts.cursor() as conn:
        _doc(conn, "a", "apple pie")  # no vector stored → dense pool empty
        hits = fts.search_hybrid(conn, query="apple", top_k=5, embedder=lambda _q: [1.0, 0.0])
        assert [h.id for h in hits] == ["a"]


def test_search_hybrid_surfaces_dense_only_semantic_hit(ac_root):
    fts.set_hybrid_config(enabled=True, recall_n=50, rrf_k=20)
    with fts.cursor() as conn:
        # "a" matches BM25 (has 'apple') AND is near the query vector.
        _doc(conn, "a", "apple pie recipe", vec=[1.0, 0.0])
        # "b" has NO lexical 'apple' but its vector is identical → dense-only hit.
        _doc(conn, "b", "fruit dessert notes", vec=[1.0, 0.0])
        # "c" is lexically + semantically unrelated.
        _doc(conn, "c", "weather forecast", vec=[0.0, 1.0])
        hits = fts.search_hybrid(conn, query="apple", top_k=5, embedder=lambda _q: [1.0, 0.0])
        ids = {h.id for h in hits}
        assert "a" in ids
        assert "b" in ids  # surfaced purely by the dense pool (BM25 alone would miss it)


def test_search_hybrid_dense_only_hit_honors_until(ac_root):
    fts.set_hybrid_config(enabled=True, recall_n=50, rrf_k=20)
    with fts.cursor() as conn:
        # dense-only hit but dated AFTER the until bound → must be filtered out.
        fts.insert_entry(
            conn,
            id="future",
            path="topic-x.md",
            prefix="topic",
            timestamp="2027-01-01T00:00:00+08:00",
            tags="",
            content="fruit notes",
        )
        vectors_mod.save_vectors(conn, [("future", [1.0, 0.0])], embedded_at="t")
        _doc(conn, "a", "apple pie", vec=[1.0, 0.0])
        hits = fts.search_hybrid(
            conn,
            query="apple",
            top_k=5,
            until="2026-12-31T00:00:00+08:00",
            embedder=lambda _q: [1.0, 0.0],
        )
        ids = {h.id for h in hits}
        assert "a" in ids
        assert "future" not in ids  # dense-only hit dropped by the until filter


def test_search_hybrid_dim_mismatch_falls_back(ac_root):
    fts.set_hybrid_config(enabled=True, recall_n=50, rrf_k=20)
    with fts.cursor() as conn:
        _doc(conn, "a", "apple pie", vec=[1.0, 0.0, 0.0])  # 3-d stored vector
        # query embeds to 2-d → dim mismatch → dense skipped → pure BM25
        hits = fts.search_hybrid(conn, query="apple", top_k=5, embedder=lambda _q: [1.0, 0.0])
        assert [h.id for h in hits] == ["a"]


def test_search_hybrid_increments_only_returned_topk(ac_root):
    fts.set_hybrid_config(enabled=True, recall_n=50, rrf_k=20)
    with fts.cursor() as conn:
        for i in range(5):
            _doc(conn, f"e{i}", "apple pie")  # all BM25-match, no vectors → dense empty
        fts.search_hybrid(conn, query="apple", top_k=2, embedder=lambda _q: [1.0, 0.0])
        counted = [eid for i in range(5) if (eid := f"e{i}") and fts.get_retrieval_count(conn, eid)]
        assert len(counted) == 2  # only the returned top-k, not the recall_n pool
