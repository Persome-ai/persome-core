"""Canonical model snapshot HTTP routes and the offline viewer shell."""

from __future__ import annotations

from datetime import UTC, datetime

from persome.api import routes
from persome.evomem.models import MemoryLayer, MemoryNode
from persome.evomem.store import NodeStore
from persome.store import fts
from persome.store import relation_edges as edges_store


def _save_point(
    *,
    node_id: str,
    content: str,
    file_name: str = "project-persome.md",
) -> None:
    NodeStore().save(
        MemoryNode(
            node_id=node_id,
            content=content,
            layer=MemoryLayer.L2_FACT,
            file_name=file_name,
            memory_at=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
        )
    )


class TestGraphJson:
    def test_graph_is_the_canonical_snapshot(self, ac_root):
        _save_point(node_id="point-runtime", content="The runtime stores local context.")

        graph = routes.model_graph()

        assert set(graph) == {"generated_at", "model"}
        assert graph["model"]["schema_version"] == 1
        assert [point["id"] for point in graph["model"]["points"]] == ["point-runtime"]
        assert set(graph["model"]) >= {
            "points",
            "lines",
            "faces",
            "volumes",
            "root",
            "receipts",
        }

    def test_empty_store_returns_an_empty_snapshot(self, ac_root):
        graph = routes.model_graph()
        assert graph["model"]["points"] == []
        assert graph["model"]["stats"]["points"] == 0


class TestViewPage:
    def test_page_uses_snapshot_native_offline_assets(self, ac_root):
        body = routes.model_view().body.decode()
        assert "/model/assets/viewer.css" in body
        assert "/model/assets/viewer.js" in body
        assert "/model/assets/three.module.js" in body
        assert "cdn.jsdelivr.net" not in body
        assert "Personal Model" in body
        assert "Points" in body and "Volumes" in body and "Root" in body

    def test_bundled_viewer_assets_are_served(self, ac_root):
        three = routes.model_asset("three.module.js")
        viewer = routes.model_asset("viewer.js")
        css = routes.model_asset("viewer.css")

        assert len(three.body) > 1_000_000
        assert b"class WebGLRenderer" in three.body
        assert b"model.points" in viewer.body
        assert b"model.lines" in viewer.body
        assert b"model.faces" in viewer.body
        assert b"model.volumes" in viewer.body
        assert b"model.root" in viewer.body
        assert viewer.media_type == "text/javascript"
        assert css.media_type == "text/css"


class TestNodeReceipts:
    def test_snapshot_point_returns_its_exact_receipt(self, ac_root):
        _save_point(
            node_id="point-runtime",
            content="The runtime stores auditable local context.",
            file_name="synthetic/runtime.md",
        )

        detail = routes.model_node(id="point-runtime")

        assert detail["source"] == "synthetic/runtime.md"
        assert detail["raw"][0]["text"] == "The runtime stores auditable local context."
        assert detail["raw"][0]["receipt"] == "⟨point-runtime:synthetic/runtime.md⟩"

    def test_historical_snapshot_point_keeps_its_receipt(self, ac_root):
        _save_point(
            node_id="point-runtime-v1",
            content="The runtime used an earlier model contract.",
            file_name="synthetic/runtime.md",
        )
        with fts.cursor() as conn:
            conn.execute(
                "UPDATE evo_nodes SET is_latest = 0, status = 'superseded' WHERE node_id = ?",
                ("point-runtime-v1",),
            )

        detail = routes.model_node(id="point-runtime-v1")

        assert detail["source"] == "synthetic/runtime.md"
        assert detail["raw"][0]["receipt"] == "⟨point-runtime-v1:synthetic/runtime.md⟩"

    def test_person_node_returns_entity_trail(self, ac_root):
        NodeStore().save(
            MemoryNode(
                node_id="n-zw-1",
                content="\u5f20\u4f1f\u8d1f\u8d23\u540e\u7aef\u8bc4\u5ba1",
                layer=MemoryLayer.L4_IDENTITY,
                file_name="person-\u5f20\u4f1f.md",
                memory_at=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
            )
        )
        detail = routes.model_node(id="\u5f20\u4f1f")
        assert detail["source"] == "person-\u5f20\u4f1f.md"
        assert (
            detail["raw"]
            and "\u5f20\u4f1f\u8d1f\u8d23\u540e\u7aef\u8bc4\u5ba1" in detail["raw"][0]["text"]
        )
        assert detail["raw"][0]["receipt"] == "⟨n-zw-1:person-\u5f20\u4f1f.md⟩"

    def test_legacy_event_node_uses_activity_adapter(self, ac_root):
        import json

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
                " 'resolved', '\u548c\u5f20\u4f1f\u5bf9\u9f50\u63a5\u53e3', ?, '[]', 'k77',"
                " '2026-07-01T09:00:00+00:00', 'done')",
                (json.dumps({"with": ["\u5f20\u4f1f"]}, ensure_ascii=False),),
            )
        detail = routes.model_node(id="event:77")
        assert detail["source"] == "⟨77:intents⟩"
        assert (
            detail["raw"]
            and "\u548c\u5f20\u4f1f\u5bf9\u9f50\u63a5\u53e3" in detail["raw"][0]["text"]
        )
        assert detail["raw"][0]["receipt"] == "⟨77:intents⟩"

    def test_unknown_id_is_empty_fail_open(self, ac_root):
        detail = routes.model_node(id="\u4e0d\u5b58\u5728\u7684\u4eba")
        assert detail["raw"] == []


class TestNodeTree:
    """A node drill-down includes a bounded strongest-first relation tree."""

    def _seed_chain(self, conn):
        for src, dst, src_kind, dst_kind, observations in (
            ("self", "\u5f20\u4f1f", "self", "person", 5),
            ("\u5f20\u4f1f", "Bob", "person", "person", 2),
            ("self", "\u674e\u56db", "self", "person", 1),
        ):
            edges_store.add_edge(
                conn,
                src_identity=src,
                dst_identity=dst,
                predicate="knows",
                src_kind=src_kind,
                dst_kind=dst_kind,
                provenance="inferred",
                confidence=0.9,
                observations=observations,
            )

    def test_tree_rooted_at_point_walks_both_directions(self, ac_root):
        with fts.cursor() as conn:
            edges_store.ensure_schema(conn)
            self._seed_chain(conn)
        tree = routes.model_node(id="\u5f20\u4f1f")["tree"]
        firsts = [
            (edge["dir"], edge["child"]["id"], edge["observations"]) for edge in tree["edges"]
        ]
        assert ("in", "self", 5) in firsts and ("out", "Bob", 2) in firsts
        self_node = next(edge["child"] for edge in tree["edges"] if edge["child"]["id"] == "self")
        second_level = {edge["child"]["id"] for edge in self_node["edges"]}
        assert "\u674e\u56db" in second_level and "\u5f20\u4f1f" not in second_level

    def test_tree_isolated_point_is_bare_root(self, ac_root):
        detail = routes.model_node(id="\u5b64\u70b9")
        assert detail["tree"] == {"id": "\u5b64\u70b9", "edges": []}
