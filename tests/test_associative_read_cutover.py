"""§5 production read cutover — `retrieval.associative.associative_read`.

The single router every query-time consumer (MCP search / writer tool-loop)
now hangs on. Deterministic, zero-LLM, zero-network (no
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
    entries_mod.create_file(
        conn, name="person-\u5f20\u4f1f.md", description="\u5f20\u4f1f", tags=["t"]
    )
    zw = entries_mod.append_entry(
        conn,
        name="person-\u5f20\u4f1f.md",
        content="\u5f20\u4f1f \u8d1f\u8d23\u652f\u4ed8\u6a21\u5757",
        tags=["fact"],
    )
    entries_mod.create_file(conn, name="project-pay.md", description="pay", tags=["t"])
    other = entries_mod.append_entry(
        conn,
        name="project-pay.md",
        content="\u652f\u4ed8\u7f51\u5173 \u5df2\u7ecf\u4e0a\u7ebf \u7070\u5ea6\u5b8c\u6210",
        tags=["fact"],
    )
    return zw, other


def _roster():
    return identity_mod.Roster.build([("\u5f20\u4f1f", [])])


class TestRouter:
    def test_slotless_query_equals_hybrid(self, ac_root):
        with fts.cursor() as conn:
            _seed(conn)
            got = assoc_mod.associative_read(
                conn, query="\u652f\u4ed8\u7f51\u5173 \u7070\u5ea6", top_k=5
            )
            want = fts.search_hybrid(conn, query="\u652f\u4ed8\u7f51\u5173 \u7070\u5ea6", top_k=5)
        assert [h.id for h in got] == [h.id for h in want]

    def test_entity_slot_reaches_who_target(self, ac_root, monkeypatch):
        # roster comes from person_graph (empty in a bare store) — inject it at
        # the same seam production uses
        monkeypatch.setattr(identity_mod, "load_roster", lambda cfg, **kw: _roster())
        with fts.cursor() as conn:
            zw, _ = _seed(conn)
            got = assoc_mod.associative_read(
                conn, query="\u5f20\u4f1f \u5728\u5fd9\u4ec0\u4e48", top_k=5
            )
        assert zw in {h.id for h in got}

    def test_kill_switch_restores_hybrid_verbatim(self, ac_root, monkeypatch):
        monkeypatch.setattr(identity_mod, "load_roster", lambda cfg, **kw: _roster())
        cfg = config_mod.load(paths.config_file())
        cfg.search.associative_read_enabled = False
        monkeypatch.setattr(config_mod, "load", lambda *a, **k: cfg)
        with fts.cursor() as conn:
            _seed(conn)
            got = assoc_mod.associative_read(
                conn, query="\u5f20\u4f1f \u5728\u5fd9\u4ec0\u4e48", top_k=5
            )
            want = fts.search_hybrid(conn, query="\u5f20\u4f1f \u5728\u5fd9\u4ec0\u4e48", top_k=5)
        assert [h.id for h in got] == [h.id for h in want]

    def test_caller_bounds_override_distilled_window(self, ac_root, monkeypatch):
        monkeypatch.setattr(identity_mod, "load_roster", lambda cfg, **kw: _roster())
        with fts.cursor() as conn:
            _seed(conn)

            # the caller's explicit bounds must win over the distilled ones
            got = assoc_mod.associative_read(
                conn,
                query="\u4eca\u5929 \u5f20\u4f1f \u5728\u5fd9\u4ec0\u4e48",
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
                dst_identity="\u5f20\u4f1f",
                predicate=edges_store.Predicate.KNOWS,
                src_kind=edges_store.EntityKind.SELF,
                dst_kind=edges_store.EntityKind.PERSON,
                provenance="inferred",
                confidence=0.9,
                status="active",
            )
            hits, chains = assoc_mod.associative_read(
                conn, query="\u5f20\u4f1f \u5728\u5fd9\u4ec0\u4e48", top_k=5, with_chains=True
            )
        assert zw in {h.id for h in hits}
        assert "\u5f20\u4f1f" in chains and "Receipts" in chains


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
                dst_identity="\u5f20\u4f1f",
                predicate=edges_store.Predicate.KNOWS,
                src_kind=edges_store.EntityKind.SELF,
                dst_kind=edges_store.EntityKind.PERSON,
                provenance="inferred",
                confidence=0.9,
                status="active",
            )
            out = mcp_server._search(conn, query="\u5f20\u4f1f \u5728\u5fd9\u4ec0\u4e48", top_k=5)
            assert zw in {r["id"] for r in out["results"]}
            assert "chains" in out and "\u5f20\u4f1f" in out["chains"]
            # archaeology mode: include_superseded is not an associative question
            out2 = mcp_server._search(
                conn,
                query="\u5f20\u4f1f \u5728\u5fd9\u4ec0\u4e48",
                top_k=5,
                include_superseded=True,
            )
            assert "chains" not in out2
