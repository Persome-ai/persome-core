"""Text-axis graded forgetting (spec 2026-07-03-text-axis-graded-forgetting).

Deterministic, fake LLM. Covers the spec §2 cell table cell by cell (fresh /
retrieved / conflicted / non-fact-prefix / floor all immune; old∧weak decays),
the §4 op shape (summary carries decayed:N + abstracted-from, sources struck
but their bytes stay in markdown = receipts), all three §5 anti-hallucination
gates, the nightly bound, idempotence, the L1→L2 one-line tier, and the
default-OFF byte equivalence.
"""

from __future__ import annotations

from types import SimpleNamespace

from persome import config as config_mod
from persome.evomem import identity as identity_mod
from persome.store import entries as entries_mod
from persome.store import files as files_mod
from persome.store import fts
from persome.writer import memory_decay as decay

OLD_TS = "2025-01-01-1000"  # far past the 90-day default window
FACTS = [
    "张伟负责支付模块的联调",
    "支付模块的联调用的是沙箱环境",
    "联调里踩过一次超时配置的坑",
    "最终结论是超时要设 30 秒",
]


class FakeDistiller:
    def __init__(self, output: str = "张伟做过支付联调，结论：超时设 30 秒。"):
        self.output = output
        self.calls = 0

    def __call__(self, _messages):
        self.calls += 1
        msg = SimpleNamespace(content=self.output, tool_calls=[])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg, finish_reason="stop")],
            usage=SimpleNamespace(total_tokens=0),
        )


def _cfg(ac_root, **overrides):
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_decay.enabled = True
    for k, v in overrides.items():
        setattr(cfg.memory_decay, k, v)
    return cfg


def _seed_old(conn, name: str, facts: list[str], *, ts: str = OLD_TS, tags=None) -> list[str]:
    """Append entries then backdate their timestamps (append_entry stamps now)."""
    entries_mod.create_file(conn, name=name, description="d", tags=["t"])
    ids = []
    for f in facts:
        ids.append(entries_mod.append_entry(conn, name=name, content=f, tags=tags or ["fact"]))
    conn.execute(
        f"UPDATE entries SET timestamp = ? WHERE id IN ({','.join('?' * len(ids))})",
        (ts, *ids),
    )
    return ids


def _live_bodies(conn, name: str) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT content FROM entries WHERE path=? AND superseded=0 ORDER BY timestamp",
            (name,),
        )
    ]


class TestCellTable:
    def test_old_weak_cluster_decays(self, ac_root):
        cfg = _cfg(ac_root)
        judge = FakeDistiller()
        with fts.cursor() as conn:
            ids = _seed_old(conn, "project-pay.md", FACTS)
            res = decay.run_memory_decay(cfg, conn, llm_call=judge)
            assert res.clusters_decayed == 1 and res.entries_retired == 4
            live = _live_bodies(conn, "project-pay.md")
            assert live == [judge.output]  # summary is the only live entry
            # op shape: decayed:1 + abstracted-from provenance on the summary
            row = conn.execute(
                "SELECT tags FROM entries WHERE path=? AND superseded=0", ("project-pay.md",)
            ).fetchone()
            assert "decayed:1" in row[0]
            assert all(i in row[0] for i in ids)  # every source id in the receipt
            # receipts: struck source bytes are STILL in the markdown file
            md = files_mod.memory_path("project-pay.md").read_text()
            for f in FACTS:
                assert f in md

    def test_fresh_entries_immune(self, ac_root):
        cfg = _cfg(ac_root)
        judge = FakeDistiller()
        with fts.cursor() as conn:
            entries_mod.create_file(conn, name="project-pay.md", description="d", tags=["t"])
            for f in FACTS:  # timestamps = now → fresh
                entries_mod.append_entry(conn, name="project-pay.md", content=f, tags=["fact"])
            res = decay.run_memory_decay(cfg, conn, llm_call=judge)
        assert judge.calls == 0 and res.clusters_considered == 0

    def test_retrieved_entries_immune(self, ac_root):
        cfg = _cfg(ac_root)
        judge = FakeDistiller()
        with fts.cursor() as conn:
            ids = _seed_old(conn, "project-pay.md", FACTS)
            fts.increment_retrieval_counts(conn, [ids[0]])  # read = reinforcement
            res = decay.run_memory_decay(cfg, conn, llm_call=judge)
            # the reinforced entry drops the cluster below cluster_min=4
            assert res.clusters_considered == 0
            assert ids[0] in [
                r[0] for r in conn.execute("SELECT id FROM entries WHERE superseded=0")
            ]

    def test_conflicted_entries_immune(self, ac_root):
        cfg = _cfg(ac_root)
        judge = FakeDistiller()
        with fts.cursor() as conn:
            ids = _seed_old(conn, "project-pay.md", FACTS)
            fts.set_entry_metadata(conn, ids[0], conflicted=True)
            res = decay.run_memory_decay(cfg, conn, llm_call=judge)
        assert res.clusters_considered == 0  # ⚠ pending human — evidence stands

    def test_non_fact_prefixes_never_scanned(self, ac_root):
        cfg = _cfg(ac_root)
        judge = FakeDistiller()
        with fts.cursor() as conn:
            _seed_old(conn, "event-2025-01-01.md", FACTS)
            _seed_old(conn, "schema-project-pay.md", FACTS)
            res = decay.run_memory_decay(cfg, conn, llm_call=judge)
        assert judge.calls == 0 and res.clusters_considered == 0

    def test_floor_tier_never_decays(self, ac_root):
        cfg = _cfg(ac_root, cluster_min=1)
        judge = FakeDistiller()
        with fts.cursor() as conn:
            _seed_old(conn, "project-pay.md", ["一行事实"], tags=["fact", "decayed:2"])
            res = decay.run_memory_decay(cfg, conn, llm_call=judge)
        assert res.clusters_considered == 0

    def test_disabled_is_noop(self, ac_root):
        cfg = _cfg(ac_root)
        cfg.memory_decay.enabled = False
        judge = FakeDistiller()
        with fts.cursor() as conn:
            _seed_old(conn, "project-pay.md", FACTS)
            res = decay.run_memory_decay(cfg, conn, llm_call=judge)
        assert judge.calls == 0 and res.clusters_decayed == 0


class TestGates:
    def _run(self, ac_root, output, **cfg_over):
        cfg = _cfg(ac_root, **cfg_over)
        judge = FakeDistiller(output)
        with fts.cursor() as conn:
            _seed_old(conn, "project-pay.md", FACTS)
            res = decay.run_memory_decay(cfg, conn, llm_call=judge)
            live = _live_bodies(conn, "project-pay.md")
        return res, live

    def test_empty_output_gated(self, ac_root):
        res, live = self._run(ac_root, "   ")
        assert res.gated == ["empty"] and live == FACTS  # cluster kept as-is

    def test_no_shrink_gated(self, ac_root):
        res, live = self._run(ac_root, "x" * 500)
        assert res.gated == ["no_shrink"] and live == FACTS

    def test_new_mention_gated(self, ac_root):
        # roster knows 李四 via a person file; the distiller "remembers" him
        # into a cluster that never mentioned him → gate
        cfg = _cfg(ac_root)
        judge = FakeDistiller("张伟和李四做过支付联调。")
        roster = identity_mod.Roster.build([("李四", [])])
        assert identity_mod.scan_mentions("李四来了", roster)  # roster sees him
        with fts.cursor() as conn:
            _seed_old(conn, "project-pay.md", FACTS)
            res = decay.run_memory_decay(cfg, conn, llm_call=judge, roster=roster)
            assert res.gated == ["new_mentions"]
            assert _live_bodies(conn, "project-pay.md") == FACTS


class TestTiersAndBounds:
    def test_l1_summary_later_decays_to_one_line(self, ac_root):
        cfg = _cfg(ac_root, cluster_min=4)
        with fts.cursor() as conn:
            # an OLD decayed:1 summary → singleton tier-2 cluster
            _seed_old(
                conn,
                "project-pay.md",
                ["张伟做过支付联调，结论：超时设 30 秒，过程曲折。"],
                tags=["fact", "decayed:1", "abstracted-from:a,b"],
            )
            judge = FakeDistiller("张伟支付联调结论：超时 30 秒")
            res = decay.run_memory_decay(cfg, conn, llm_call=judge)
            assert res.clusters_decayed == 1
            row = conn.execute(
                "SELECT tags FROM entries WHERE path=? AND superseded=0", ("project-pay.md",)
            ).fetchone()
            assert "decayed:2" in row[0]

    def test_l2_one_line_gate(self, ac_root):
        cfg = _cfg(ac_root)
        with fts.cursor() as conn:
            _seed_old(
                conn,
                "project-pay.md",
                ["一段很长很长的 decayed:1 摘要，讲了支付联调的种种。"],
                tags=["fact", "decayed:1"],
            )
            judge = FakeDistiller("第一行\n第二行")  # multi-line → gated
            res = decay.run_memory_decay(cfg, conn, llm_call=judge)
        assert res.gated == ["not_one_line"]

    def test_nightly_bound(self, ac_root):
        cfg = _cfg(ac_root, max_clusters_per_night=1)
        judge = FakeDistiller()
        with fts.cursor() as conn:
            _seed_old(conn, "project-a.md", FACTS)
            _seed_old(conn, "project-b.md", FACTS)
            res = decay.run_memory_decay(cfg, conn, llm_call=judge)
        assert judge.calls == 1 and res.clusters_decayed == 1

    def test_idempotent_next_night(self, ac_root):
        cfg = _cfg(ac_root)
        judge = FakeDistiller()
        with fts.cursor() as conn:
            _seed_old(conn, "project-pay.md", FACTS)
            decay.run_memory_decay(cfg, conn, llm_call=judge)
            res2 = decay.run_memory_decay(cfg, conn, llm_call=judge)
        # sources retired + fresh summary is young → nothing to do tonight
        assert judge.calls == 1 and res2.clusters_considered == 0

    def test_cluster_min_respected(self, ac_root):
        cfg = _cfg(ac_root, cluster_min=4)
        judge = FakeDistiller()
        with fts.cursor() as conn:
            _seed_old(conn, "project-pay.md", FACTS[:3])  # only 3 old details
            res = decay.run_memory_decay(cfg, conn, llm_call=judge)
        assert judge.calls == 0 and res.clusters_considered == 0

    def test_bad_llm_reply_fail_open(self, ac_root):
        cfg = _cfg(ac_root)

        def broken(_messages):
            raise RuntimeError("gateway down")

        with fts.cursor() as conn:
            _seed_old(conn, "project-pay.md", FACTS)
            res = decay.run_memory_decay(cfg, conn, llm_call=broken)
            assert res.clusters_decayed == 0
            assert _live_bodies(conn, "project-pay.md") == FACTS
