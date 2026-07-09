"""Tree-chain delivery (§3.4) — beam search, prefix merge, receipts, budget.

Zero LLM. Pins: bottleneck scoring picks the stronger path; shadow edges never
carry a chain; orphans are flagged honestly; prefix merge renders the shared
root once; budget compression NEVER drops a chain; walked edges get their read
reinforcement (recall_count).
"""

from __future__ import annotations

from persome.evomem import identity
from persome.evomem.models import MemoryStatus
from persome.retrieval import chains as chains_mod
from persome.store import fts
from persome.store import relation_edges as edges_store
from persome.store.fts import EntryHit


def _edge(conn, src, dst, obs=1, status=MemoryStatus.ACTIVE):
    return edges_store.add_edge(
        conn,
        src_identity=src,
        dst_identity=dst,
        predicate="knows",
        src_kind="person",
        dst_kind="person",
        provenance="user_committed",
        confidence=0.9,
        status=status,
        valid_from="2026-06-01T00:00:00+08:00",
        observations=obs,
    )


def test_beam_picks_bottleneck_strongest_path(ac_root):
    with fts.cursor() as conn:
        edges_store.ensure_schema(conn)
        # two routes Bob→USER: via 张伟 (bottleneck 2) vs via Carol (bottleneck 1)
        _edge(conn, "self", "张伟", obs=3)
        _edge(conn, "张伟", "Bob", obs=2)
        _edge(conn, "self", "Carol", obs=5)
        _edge(conn, "Carol", "Bob", obs=1)
        chain = chains_mod.chain_to_user(conn, "Bob")
        assert chain is not None
        assert chain.identities == ["self", "张伟", "Bob"]  # root-first, stronger route
        assert chain.score == 2  # the weakest hop decides


def test_shadow_edges_never_carry_a_chain_and_orphans_are_honest(ac_root):
    with fts.cursor() as conn:
        edges_store.ensure_schema(conn)
        _edge(conn, "self", "王五", obs=9, status=MemoryStatus.SHADOW)
        assert chains_mod.chain_to_user(conn, "王五") is None  # shadow ≠ proven
        assert chains_mod.chain_to_user(conn, "无名氏") is None


def test_pull_chains_merges_prefix_reinforces_and_receipts(ac_root):
    roster = identity.Roster.build([("张伟", []), ("Bob", []), ("Carol", []), ("Lily", [])])
    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        edges_store.ensure_schema(conn)
        _edge(conn, "self", "张伟", obs=3)
        _edge(conn, "张伟", "Bob", obs=2)
        _edge(conn, "张伟", "Carol", obs=2)
        hits = [
            EntryHit(
                id="e1", path="person-bob.md", timestamp="", content="Bob 和 Carol 都在", rank=0
            ),
            EntryHit(id="e2", path="person-lily.md", timestamp="", content="Lily 发了邮件", rank=0),
        ]
        delivery = chains_mod.pull_chains(conn, hits, roster)
        # receipts: one pointer per hit — the §2.1 disclosure handles
        assert ("e1", "person-bob.md") in delivery.receipts
        # Bob and Carol both chain through 张伟 — the shared prefix renders ONCE
        assert sum("张伟" in ln for ln in delivery.lines) == 1
        assert set(delivery.chained_anchors) == {"Bob", "Carol"}
        assert delivery.orphan_anchors == ["Lily"]  # no edges — honest orphan
        # read is reinforcement: every walked edge got recall_count += 1
        counts = dict(
            conn.execute(
                "SELECT edge_id, recall_count FROM relation_edges WHERE recall_count > 0"
            ).fetchall()
        )
        assert set(counts) == set(delivery.walked_edge_ids) and len(counts) == 3
        assert counts[delivery.walked_edge_ids[0]] >= 1


def test_render_budget_compresses_but_never_cuts(ac_root):
    roster = identity.Roster.build([("张伟", []), ("Bob", [])])
    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        edges_store.ensure_schema(conn)
        _edge(conn, "self", "张伟", obs=3)
        _edge(conn, "张伟", "Bob", obs=2)
        hits = [EntryHit(id="e1", path="person-bob.md", timestamp="", content="Bob 值班", rank=0)]
        delivery = chains_mod.pull_chains(conn, hits, roster)
        full = chains_mod.render_delivery(delivery, budget_chars=4000)
        assert "强度" in full and "⟨e1:person-bob.md⟩" in full
        tight = chains_mod.render_delivery(delivery, budget_chars=60)
        # compressed: annotations gone, but the chain endpoint AND receipts survive
        assert "Bob" in tight and "⟨e1:person-bob.md⟩" in tight
        assert "强度" not in tight
