"""§5 production read cutover — `retrieval.associative.associative_read`.

The single router every query-time consumer (MCP search / chat memory tool /
writer tool-loop) now hangs on. Deterministic, zero-LLM, zero-network (no
embedder → dense pool inert; the associative entrance's slot-less degrade IS
search_hybrid, so parity is structural, and these tests pin the router's own
contracts: kill-switch verbatim fallback, caller-bound precedence, the slot
path actually reaching entity-only targets, and the MCP chains field).
"""

from __future__ import annotations

from persome import config as config_mod
from persome import paths
from persome.evomem import identity as identity_mod
from persome.retrieval import associative as assoc_mod
from persome.store import entries as entries_mod
from persome.store import fts
from persome.store import relation_edges as edges_store


def _seed(conn):
    entries_mod.create_file(conn, name="person-张伟.md", description="张伟", tags=["t"])
    zw = entries_mod.append_entry(
        conn, name="person-张伟.md", content="张伟 负责支付模块", tags=["fact"]
    )
    entries_mod.create_file(conn, name="project-pay.md", description="pay", tags=["t"])
    other = entries_mod.append_entry(
        conn, name="project-pay.md", content="支付网关 已经上线 灰度完成", tags=["fact"]
    )
    return zw, other


def _roster():
    return identity_mod.Roster.build([("张伟", [])])


class TestRouter:
    def test_slotless_query_equals_hybrid(self, ac_root):
        with fts.cursor() as conn:
            _seed(conn)
            got = assoc_mod.associative_read(conn, query="支付网关 灰度", top_k=5)
            want = fts.search_hybrid(conn, query="支付网关 灰度", top_k=5)
        assert [h.id for h in got] == [h.id for h in want]

    def test_entity_slot_reaches_who_target(self, ac_root, monkeypatch):
        # roster comes from person_graph (empty in a bare store) — inject it at
        # the same seam production uses
        monkeypatch.setattr(identity_mod, "load_roster", lambda cfg, **kw: _roster())
        with fts.cursor() as conn:
            zw, _ = _seed(conn)
            got = assoc_mod.associative_read(conn, query="张伟 在忙什么", top_k=5)
        assert zw in {h.id for h in got}

    def test_kill_switch_restores_hybrid_verbatim(self, ac_root, monkeypatch):
        monkeypatch.setattr(identity_mod, "load_roster", lambda cfg, **kw: _roster())
        cfg = config_mod.load(paths.config_file())
        cfg.search.associative_read_enabled = False
        monkeypatch.setattr(config_mod, "load", lambda *a, **k: cfg)
        with fts.cursor() as conn:
            _seed(conn)
            got = assoc_mod.associative_read(conn, query="张伟 在忙什么", top_k=5)
            want = fts.search_hybrid(conn, query="张伟 在忙什么", top_k=5)
        assert [h.id for h in got] == [h.id for h in want]

    def test_caller_bounds_override_distilled_window(self, ac_root, monkeypatch):
        monkeypatch.setattr(identity_mod, "load_roster", lambda cfg, **kw: _roster())
        with fts.cursor() as conn:
            _seed(conn)
            # 今天…would distill a window excluding the (older) seeded entries;
            # the caller's explicit bounds must win over the distilled ones
            got = assoc_mod.associative_read(
                conn,
                query="今天 张伟 在忙什么",
                top_k=5,
                since="2000-01-01",
                until="2999-01-01",
            )
        assert got  # caller window keeps the corpus reachable

    def test_with_chains_returns_narrative_over_active_edges(self, ac_root, monkeypatch):
        monkeypatch.setattr(identity_mod, "load_roster", lambda cfg, **kw: _roster())
        with fts.cursor() as conn:
            zw, _ = _seed(conn)
            edges_store.ensure_schema(conn)
            edges_store.add_edge(
                conn,
                src_identity="self",
                dst_identity="张伟",
                predicate=edges_store.Predicate.KNOWS,
                src_kind=edges_store.EntityKind.SELF,
                dst_kind=edges_store.EntityKind.PERSON,
                provenance="inferred",
                confidence=0.9,
                status="active",
            )
            hits, chains = assoc_mod.associative_read(
                conn, query="张伟 在忙什么", top_k=5, with_chains=True
            )
        assert zw in {h.id for h in hits}
        assert "张伟" in chains and "收据" in chains  # narrative + receipt pointers


class TestMcpSearchCutover:
    def test_search_returns_chains_field_and_supersede_mode_stays_hybrid(
        self, ac_root, monkeypatch
    ):
        from persome.mcp import server as mcp_server

        monkeypatch.setattr(identity_mod, "load_roster", lambda cfg, **kw: _roster())
        with fts.cursor() as conn:
            zw, _ = _seed(conn)
            edges_store.ensure_schema(conn)
            edges_store.add_edge(
                conn,
                src_identity="self",
                dst_identity="张伟",
                predicate=edges_store.Predicate.KNOWS,
                src_kind=edges_store.EntityKind.SELF,
                dst_kind=edges_store.EntityKind.PERSON,
                provenance="inferred",
                confidence=0.9,
                status="active",
            )
            out = mcp_server._search(conn, query="张伟 在忙什么", top_k=5)
            assert zw in {r["id"] for r in out["results"]}
            assert "chains" in out and "张伟" in out["chains"]
            # archaeology mode: include_superseded is not an associative question
            out2 = mcp_server._search(conn, query="张伟 在忙什么", top_k=5, include_superseded=True)
            assert "chains" not in out2
