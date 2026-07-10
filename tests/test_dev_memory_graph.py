"""§7-6 记忆图 — /dev/memory-graph JSON + /dev/memory view (dev-gated).

Deterministic, zero-LLM. Pins: the dev gate (404 when off — indistinguishable
from absent), the graph JSON contract (nodes derived from USER ∪ edge
endpoints ∪ roster, kind partition user/person/activity, edges carrying their
bitemporal fields both statuses included, faces live-only), the served page
(three.js canvas fetching the data endpoint), the rebuild-free override, and
the dashboard's 记忆图 tab wiring (lazy iframe).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from persome import paths
from persome.api import routes
from persome.store import fts
from persome.store import relation_edges as edges_store
from persome.store import schema_faces as faces_store


@pytest.fixture()
def dev_on(monkeypatch):
    monkeypatch.setattr(routes, "_dev_enabled", lambda: True)


def _seed(conn):
    edges_store.ensure_schema(conn)
    faces_store.ensure_schema(conn)
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
        valid_from="2026-01-01T00:00:00+00:00",
    )
    edges_store.add_edge(
        conn,
        src_identity="self",
        dst_identity="event:42",
        predicate=edges_store.Predicate.PARTICIPATES_IN,
        src_kind=edges_store.EntityKind.SELF,
        dst_kind=edges_store.EntityKind.EVENT,
        provenance="inferred",
        confidence=0.9,
    )
    fid = faces_store.record_face(
        conn,
        source="mined",
        signature="每天早上先看邮件",
        members=["a", "b", "c"],
        anchors=["张伟"],
    )
    return fid


class TestGate:
    def test_both_routes_404_when_dev_off(self, ac_root, monkeypatch):
        monkeypatch.setattr(routes, "_dev_enabled", lambda: False)
        for fn in (routes.dev_memory_graph, routes.dev_memory_view):
            with pytest.raises(HTTPException) as exc:
                fn()
            assert exc.value.status_code == 404


class TestGraphJson:
    def test_shape_nodes_edges_faces(self, ac_root, dev_on):
        with fts.cursor() as conn:
            _seed(conn)
        g = routes.dev_memory_graph()
        # node kind = the full EntityKind closed set (§1.2 种类 axis), recovered
        # from the persisted src_kind/dst_kind edge columns
        kinds = {n["id"]: n["kind"] for n in g["nodes"]}
        assert kinds["self"] == "self"
        assert kinds["张伟"] == "person"
        assert kinds["event:42"] == "event"
        # edges carry status + bitemporal fields; BOTH statuses included (the
        # shadow/ACTIVE split is what the view exists to show)
        stats = {e["status"] for e in g["edges"]}
        assert stats == {"active", "shadow"}
        active = next(e for e in g["edges"] if e["status"] == "active")
        assert active["valid_from"] == "2026-01-01T00:00:00+00:00"
        assert "observations" in active and "recall_count" in active
        # §7-6 lens axes ride along: kinds, polarity (closed ±0), provenance
        assert active["src_kind"] == "self" and active["dst_kind"] == "person"
        assert active["polarity"] == "0"
        assert active["provenance"] == "inferred"
        assert [f["provenance"] for f in g["faces"]] == ["mined"]
        assert g["faces"][0]["level"] == 1
        # anchors ride along — the hull vertices the view renders the face over
        assert g["faces"][0]["anchors"] == ["张伟"]
        # §7-8 检索权重状态随行（看板 stats 行的数据源）
        s = g["search"]
        assert set(s) == {
            "slot_pool_weight",
            "relation_pool_weight",
            "relation_include_shadow",
            "contains_pool_rerank",
            "active_edges",
            "shadow_edges",
        }
        assert s["active_edges"] == 1 and s["shadow_edges"] == 1
        # §7-10 池内混排状态随行 — 模块默认开
        assert s["contains_pool_rerank"] is True

    def test_empty_store_is_user_only_fail_open(self, ac_root, dev_on):
        g = routes.dev_memory_graph()
        assert [n["id"] for n in g["nodes"]] == ["self"]
        assert g["edges"] == [] and g["faces"] == []


class TestViewPage:
    def test_page_serves_the_canvas(self, ac_root, dev_on):
        body = routes.dev_memory_view().body.decode()
        assert "/dev/memory-graph" in body  # fetches the real data
        assert "three@0.160.0" in body  # the ontology-three canvas
        assert "as-of" in body  # the f(T) scrubber

    def test_rebuild_free_override(self, ac_root, dev_on):
        (paths.root() / "dev_memory.html").write_text("<html>OVERRIDE</html>")
        assert routes.dev_memory_view().body.decode() == "<html>OVERRIDE</html>"


class TestNodeReceipts:
    """§2.1 click-through: /dev/memory-node returns the raw receipts behind a point."""

    def test_gate_404_when_dev_off(self, ac_root, monkeypatch):
        monkeypatch.setattr(routes, "_dev_enabled", lambda: False)
        with pytest.raises(HTTPException) as exc:
            routes.dev_memory_node(id="张伟")
        assert exc.value.status_code == 404

    def test_person_node_returns_entity_trail(self, ac_root, dev_on):
        from datetime import UTC, datetime

        from persome.evomem.models import MemoryLayer, MemoryNode
        from persome.evomem.store import NodeStore

        NodeStore().save(
            MemoryNode(
                node_id="n-zw-1",
                content="张伟负责后端评审",
                layer=MemoryLayer.L4_IDENTITY,
                file_name="person-张伟.md",
                memory_at=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
            )
        )
        d = routes.dev_memory_node(id="张伟")
        assert d["source"] == "person-张伟.md"
        assert d["raw"] and "张伟负责后端评审" in d["raw"][0]["text"]

    def test_event_node_returns_minting_intent(self, ac_root, dev_on):
        import json as _json

        from persome.intent import store as intent_store

        with fts.cursor() as conn:
            intent_store.ensure_schema(conn)
            conn.execute(
                "INSERT INTO intents (id, ts, scope, kind, confidence, status, rationale,"
                " payload, evidence, dedup_key, created_at)"
                " VALUES (77, '2026-07-01T09:00:00+00:00', 'timeline', 'meeting', 0.9,"
                " 'resolved', '和张伟对齐接口', ?, '[]', 'k77', '2026-07-01T09:00:00+00:00')",
                (_json.dumps({"with": ["张伟"]}, ensure_ascii=False),),
            )
        d = routes.dev_memory_node(id="event:77")
        assert d["source"] == "intents"
        assert d["raw"] and "和张伟对齐接口" in d["raw"][0]["text"]
        assert "张伟" in d["raw"][0]["text"]

    def test_unknown_id_is_empty_fail_open(self, ac_root, dev_on):
        d = routes.dev_memory_node(id="不存在的人")
        assert d["raw"] == []


class TestTypedPoints:
    """§7-6 kind-axis 过渡腿: org-*/project-* entity files enter as typed points."""

    def test_project_entity_file_becomes_project_node(self, ac_root, dev_on):
        from datetime import UTC, datetime

        from persome.evomem.models import MemoryLayer, MemoryNode
        from persome.evomem.store import NodeStore

        NodeStore().save(
            MemoryNode(
                node_id="n-acme-1",
                content="Acme 是主项目",
                layer=MemoryLayer.L4_IDENTITY,
                file_name="project-Acme.md",
                memory_at=datetime(2026, 7, 1, tzinfo=UTC),
            )
        )
        g = routes.dev_memory_graph()
        kinds = {n["id"]: n["kind"] for n in g["nodes"]}
        # a typed point with no edges is an honest orphan, but it EXISTS with
        # its kind — the sector can start growing
        assert kinds.get("Acme") == "project"


class TestNodeTree:
    """§1.2 点开一个事物 → 以它为根的整棵树 (bounded BFS, strongest-first)."""

    def _seed_chain(self, conn):
        for src, dst, sk, dk, obs in (
            ("self", "张伟", "self", "person", 5),
            ("张伟", "Bob", "person", "person", 2),
            ("self", "李四", "self", "person", 1),
        ):
            edges_store.add_edge(
                conn,
                src_identity=src,
                dst_identity=dst,
                predicate="knows",
                src_kind=sk,
                dst_kind=dk,
                provenance="inferred",
                confidence=0.9,
                observations=obs,
            )

    def test_tree_rooted_at_point_walks_both_directions(self, ac_root, dev_on):
        with fts.cursor() as conn:
            edges_store.ensure_schema(conn)
            self._seed_chain(conn)
        d = routes.dev_memory_node(id="张伟")
        tree = d["tree"]
        assert tree["id"] == "张伟"
        # depth 1: incoming self edge (obs 5) first, then outgoing Bob
        firsts = [(e["dir"], e["child"]["id"], e["observations"]) for e in tree["edges"]]
        assert ("in", "self", 5) in firsts and ("out", "Bob", 2) in firsts
        # depth 2 under self: 李四 reachable; 张伟 cycle-guarded out
        self_node = next(e["child"] for e in tree["edges"] if e["child"]["id"] == "self")
        ids2 = {e["child"]["id"] for e in self_node["edges"]}
        assert "李四" in ids2 and "张伟" not in ids2

    def test_tree_isolated_point_is_bare_root(self, ac_root, dev_on):
        d = routes.dev_memory_node(id="孤点")
        assert d["tree"] == {"id": "孤点", "edges": []}
