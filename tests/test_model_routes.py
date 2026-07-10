"""Formal paper-model HTTP routes and local 3D visualization.

Deterministic, zero-LLM. Pins: the graph JSON contract (nodes derived from USER ∪ edge
endpoints ∪ roster, kind partition user/person/activity, edges carrying their
bitemporal fields both statuses included, faces live-only), the served page
(locally bundled Three.js canvas fetching the model endpoint), versioned model
snapshot, receipt drill-down, and offline assets.
"""

from __future__ import annotations

from persome.api import routes
from persome.store import fts
from persome.store import relation_edges as edges_store
from persome.store import schema_faces as faces_store


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
        dst_identity="event:entry:e42",
        predicate=edges_store.Predicate.PARTICIPATES_IN,
        src_kind=edges_store.EntityKind.SELF,
        dst_kind=edges_store.EntityKind.EVENT,
        provenance="inferred",
        confidence=0.9,
        source_kind="entry",
        source_id="e42",
        source_receipt="⟨e42:event-2026-07-10.md⟩",
    )
    fid = faces_store.record_face(
        conn,
        source="mined",
        signature="每天早上先看邮件",
        members=["a", "b", "c"],
        anchors=["张伟"],
    )
    return fid


class TestGraphJson:
    def test_shape_nodes_edges_faces(self, ac_root):
        with fts.cursor() as conn:
            _seed(conn)
        g = routes.model_graph()
        # node kind = the full EntityKind closed set (§1.2 种类 axis), recovered
        # from the persisted src_kind/dst_kind edge columns
        kinds = {n["id"]: n["kind"] for n in g["nodes"]}
        assert kinds["self"] == "self"
        assert kinds["张伟"] == "person"
        assert kinds["event:entry:e42"] == "event"
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
        activity = next(e for e in g["edges"] if e["b"] == "event:entry:e42")
        assert activity["source_kind"] == "entry"
        assert activity["source_id"] == "e42"
        assert activity["source_receipt"] == "⟨e42:event-2026-07-10.md⟩"
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
        assert g["model"]["schema_version"] == 1
        assert set(g["model"]) >= {"points", "lines", "faces", "volumes", "root", "receipts"}

    def test_empty_store_is_user_only_fail_open(self, ac_root):
        g = routes.model_graph()
        assert [n["id"] for n in g["nodes"]] == ["self"]
        assert g["edges"] == [] and g["faces"] == []
        assert g["model"]["stats"]["points"] == 0


class TestViewPage:
    def test_page_serves_the_canvas_without_network_imports(self, ac_root):
        body = routes.model_view().body.decode()
        assert "/model/graph" in body
        assert "/model/assets/three.module.js" in body
        assert "cdn.jsdelivr.net" not in body
        assert "preserveDrawingBuffer:true" in body
        assert "point.receipt" in body
        assert "根◎" in body
        assert "as-of" in body  # the f(T) scrubber

    def test_bundled_three_asset_is_served(self, ac_root):
        response = routes.model_asset("three.module.js")
        assert len(response.body) > 1_000_000
        assert b"class WebGLRenderer" in response.body


class TestNodeReceipts:
    """§2.1 click-through: /model/node returns raw receipts behind a point."""

    def test_person_node_returns_entity_trail(self, ac_root):
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
        d = routes.model_node(id="张伟")
        assert d["source"] == "person-张伟.md"
        assert d["raw"] and "张伟负责后端评审" in d["raw"][0]["text"]
        assert d["raw"][0]["receipt"] == "⟨n-zw-1:person-张伟.md⟩"

    def test_legacy_event_node_uses_activity_adapter(self, ac_root):
        import json as _json

        with fts.cursor() as conn:
            conn.execute(
                """
                CREATE TABLE intents (
                    id INTEGER PRIMARY KEY,
                    ts TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    status TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    dedup_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolution_outcome TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO intents (id, ts, scope, kind, confidence, status, rationale,"
                " payload, evidence, dedup_key, created_at, resolution_outcome)"
                " VALUES (77, '2026-07-01T09:00:00+00:00', 'timeline', 'meeting', 0.9,"
                " 'resolved', '和张伟对齐接口', ?, '[]', 'k77',"
                " '2026-07-01T09:00:00+00:00', 'done')",
                (_json.dumps({"with": ["张伟"]}, ensure_ascii=False),),
            )
        d = routes.model_node(id="event:77")
        assert d["source"] == "⟨77:intents⟩"
        assert d["raw"] and "和张伟对齐接口" in d["raw"][0]["text"]
        assert d["raw"][0]["receipt"] == "⟨77:intents⟩"

    def test_unknown_id_is_empty_fail_open(self, ac_root):
        d = routes.model_node(id="不存在的人")
        assert d["raw"] == []


class TestTypedPoints:
    """§7-6 kind-axis 过渡腿: org-*/project-* entity files enter as typed points."""

    def test_project_entity_file_becomes_project_node(self, ac_root):
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
        g = routes.model_graph()
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

    def test_tree_rooted_at_point_walks_both_directions(self, ac_root):
        with fts.cursor() as conn:
            edges_store.ensure_schema(conn)
            self._seed_chain(conn)
        d = routes.model_node(id="张伟")
        tree = d["tree"]
        assert tree["id"] == "张伟"
        # depth 1: incoming self edge (obs 5) first, then outgoing Bob
        firsts = [(e["dir"], e["child"]["id"], e["observations"]) for e in tree["edges"]]
        assert ("in", "self", 5) in firsts and ("out", "Bob", 2) in firsts
        # depth 2 under self: 李四 reachable; 张伟 cycle-guarded out
        self_node = next(e["child"] for e in tree["edges"] if e["child"]["id"] == "self")
        ids2 = {e["child"]["id"] for e in self_node["edges"]}
        assert "李四" in ids2 and "张伟" not in ids2

    def test_tree_isolated_point_is_bare_root(self, ac_root):
        d = routes.model_node(id="孤点")
        assert d["tree"] == {"id": "孤点", "edges": []}
