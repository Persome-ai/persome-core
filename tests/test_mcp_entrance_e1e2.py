"Tests for test mcp entrance e1e2."

from __future__ import annotations

import pytest

from persome.evomem import identity as identity_mod
from persome.mcp import server as mcp_server
from persome.retrieval import associative as assoc_mod
from persome.store import fts
from persome.store import relation_edges as edges_store


@pytest.fixture()
def _roster_zhangwei(monkeypatch):
    roster = identity_mod.Roster.build([("\u5f20\u4f1f", ["\u4f1f\u54e5"]), ("self", [])])
    monkeypatch.setattr(identity_mod, "load_roster", lambda cfg, **kw: roster)
    return roster


def _insert(conn, *, id: str, ts: str, content: str, path: str = "topic-x.md"):
    conn.execute(
        "INSERT INTO entries (id, path, prefix, timestamp, tags, content, superseded)"
        " VALUES (?, ?, 'topic', ?, '', ?, 0)",
        (id, path, ts, content),
    )


def test_breadth_trades_redundancy_for_coverage(ac_root):
    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        _insert(
            conn,
            id="e-dup1",
            ts="2026-06-01T10:00",
            content="\u90e8\u7f72 \u6d41\u7a0b \u68c0\u67e5\u5355 \u6b65\u9aa4 \u4e00",
        )
        _insert(
            conn,
            id="e-dup2",
            ts="2026-06-01T10:01",
            content="\u90e8\u7f72 \u6d41\u7a0b \u68c0\u67e5\u5355 \u6b65\u9aa4 \u4e8c",
        )
        _insert(
            conn,
            id="e-dup3",
            ts="2026-06-01T10:02",
            content="\u90e8\u7f72 \u6d41\u7a0b \u68c0\u67e5\u5355 \u6b65\u9aa4 \u4e09",
        )
        _insert(
            conn,
            id="e-alt",
            ts="2026-06-01T10:03",
            content="\u90e8\u7f72 \u56de\u6eda \u9884\u6848 \u5b8c\u5168 \u4e0d\u540c \u5185\u5bb9",
        )
        narrow = [h.id for h in fts.search_hybrid(conn, query="\u90e8\u7f72 \u6d41\u7a0b", top_k=2)]
        assert "e-alt" not in narrow
        wide = [
            h.id
            for h in fts.search_hybrid(
                conn, query="\u90e8\u7f72 \u6d41\u7a0b", top_k=2, mmr_diversity=0.8
            )
        ]
        assert "e-alt" in wide

        assert set(wide) <= {"e-dup1", "e-dup2", "e-dup3", "e-alt"}


def test_breadth_survives_slotless_degrade_via_associative_read(ac_root, _roster_zhangwei):
    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        _insert(
            conn,
            id="e-dup1",
            ts="2026-06-01T10:00",
            content="\u90e8\u7f72 \u6d41\u7a0b \u68c0\u67e5\u5355 \u6b65\u9aa4 \u4e00",
        )
        _insert(
            conn,
            id="e-dup2",
            ts="2026-06-01T10:01",
            content="\u90e8\u7f72 \u6d41\u7a0b \u68c0\u67e5\u5355 \u6b65\u9aa4 \u4e8c",
        )
        _insert(
            conn,
            id="e-alt",
            ts="2026-06-01T10:02",
            content="\u90e8\u7f72 \u56de\u6eda \u9884\u6848 \u5b8c\u5168 \u4e0d\u540c \u5185\u5bb9",
        )
        hits = assoc_mod.associative_read(
            conn, query="\u90e8\u7f72 \u6d41\u7a0b", top_k=2, mmr_diversity=0.8
        )
        assert "e-alt" in [h.id for h in hits]


def test_explicit_entities_arm_the_who_head(ac_root, _roster_zhangwei):
    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        _insert(
            conn,
            id="e-zw",
            ts="2026-06-01T10:00",
            content="\u5f20\u4f1f \u8d1f\u8d23 \u53d1\u7248 \u4e8b\u5b9c",
        )
        _insert(
            conn,
            id="e-noise",
            ts="2026-06-01T10:01",
            content="\u5b8c\u5168 \u65e0\u5173 \u7684 \u5185\u5bb9",
        )
        hits = assoc_mod.associative_read(
            conn, query="\u90a3\u4ef6\u4e8b\u8c01\u5728\u8ddf", top_k=3, entities=["\u4f1f\u54e5"]
        )
        assert "e-zw" in [h.id for h in hits]

        hits2 = assoc_mod.associative_read(
            conn,
            query="\u90a3\u4ef6\u4e8b\u8c01\u5728\u8ddf",
            top_k=3,
            entities=["\u4e0d\u5b58\u5728\u7684\u4eba"],
        )
        assert isinstance(hits2, list)


def test_read_receipt_dereferences_and_breadcrumbs(ac_root):
    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        _insert(
            conn,
            id="e-r1",
            ts="2026-06-01T10:00:00",
            content="\u53d1\u7248 0.3.9 \u5df2\u51fa\u5305",
        )
        conn.execute(
            "INSERT INTO captures (id, timestamp, app_name, window_title, visible_text)"
            " VALUES ('cap-1', '2026-06-01T10:05:00', 'Feishu', '\u53d1\u7248\u7fa4', '\u51fa\u5305\u4e86')"
        )
        out = mcp_server._read_receipt(conn, entry_id="e-r1")
        assert out["id"] == "e-r1" and out["superseded"] is False
        assert out["content"].startswith("\u53d1\u7248")
        assert isinstance(out["age_days"], int)
        assert [c["id"] for c in out["nearby_captures"]] == ["cap-1"]

        assert fts.get_retrieval_count(conn, "e-r1") == 1


def test_read_receipt_honest_miss_and_superseded_label(ac_root):
    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        assert "error" in mcp_server._read_receipt(conn, entry_id="nope")
        conn.execute(
            "INSERT INTO entries (id, path, prefix, timestamp, tags, content, superseded)"
            " VALUES ('e-old', 'topic-x.md', 'topic', '2026-05-01T10:00', '', '\u65e7\u7248\u672c \u4e8b\u5b9e', 1)"
        )
        out = mcp_server._read_receipt(conn, entry_id="e-old")
        assert out["superseded"] is True
        assert fts.get_retrieval_count(conn, "e-old") == 0


def _seed_edges(conn):
    edges_store.ensure_schema(conn)
    edges_store.add_edge(
        conn,
        src_identity="self",
        dst_identity="\u5f20\u4f1f",
        predicate="knows",
        src_kind="self",
        dst_kind="person",
        provenance="user_committed",
        confidence=0.9,
        status="active",
        valid_from="2026-05-01T00:00",
        quote="q",
    )
    edges_store.add_edge(
        conn,
        src_identity="\u5f20\u4f1f",
        dst_identity="\u674e\u8001\u677f",
        predicate="reports_to",
        src_kind="person",
        dst_kind="person",
        provenance="user_committed",
        confidence=0.9,
        status="active",
        valid_from="2026-05-01T00:00",
        quote="q",
    )
    edges_store.add_edge(
        conn,
        src_identity="\u5f20\u4f1f",
        dst_identity="\u795e\u79d8\u4eba",
        predicate="knows",
        src_kind="person",
        dst_kind="person",
        provenance="inferred",
        confidence=0.8,
        status="shadow",
        valid_from="2026-05-01T00:00",
        quote="q",
    )


def test_entity_graph_edges_neighbors_and_chain(ac_root, _roster_zhangwei):
    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        _seed_edges(conn)
        out = mcp_server._entity_graph(conn, None, name="\u4f1f\u54e5", depth=2)
        assert out["resolved"] == "\u5f20\u4f1f"
        preds = {e["predicate"] for e in out["edges"]}
        assert preds == {"knows", "reports_to"}  # ACTIVE only
        assert (
            "\u674e\u8001\u677f" in out["neighbors"]
            and "\u795e\u79d8\u4eba" not in out["neighbors"]
        )
        assert out["chain_to_user"] and "\u5f20\u4f1f" in out["chain_to_user"]
        with_shadow = mcp_server._entity_graph(
            conn, None, name="\u5f20\u4f1f", depth=2, include_shadow=True
        )
        assert any(e["dst"] == "\u795e\u79d8\u4eba" for e in with_shadow["shadow_edges"])
        assert "\u795e\u79d8\u4eba" in with_shadow["neighbors"]


def test_entity_graph_as_of_time_travel(ac_root, _roster_zhangwei):
    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        _seed_edges(conn)
        out = mcp_server._entity_graph(conn, None, name="\u5f20\u4f1f", as_of="2026-04-01T00:00")
        assert out["edges"] == []
        out2 = mcp_server._entity_graph(conn, None, name="\u5f20\u4f1f", as_of="2026-06-01T00:00")
        assert len(out2["edges"]) == 2


def test_entity_graph_honest_miss_for_unknown_name(ac_root, _roster_zhangwei):
    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        out = mcp_server._entity_graph(
            conn, None, name="\u5b8c\u5168\u964c\u751f\u7684\u540d\u5b57"
        )
        assert out["resolved"] is None
        assert "layer" in out and "note" in out


def test_search_hits_carry_covering_faces(ac_root, _roster_zhangwei):
    from persome.store import schema_faces as faces_store

    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        fact = (
            "\u7528\u6237 \u575a\u6301 \u6570\u636e \u9a71\u52a8 \u7684 \u53d1\u7248 \u6d41\u7a0b"
        )
        _insert(conn, id="e-fact", ts="2026-06-01T10:00", content=fact)
        _insert(
            conn,
            id="e-plain",
            ts="2026-06-01T10:01",
            content="\u53d1\u7248 \u65e0\u5173 \u5907\u6ce8",
        )
        face_id = faces_store.record_face(
            conn,
            source="mined",
            signature="\u7528\u6237\u4ee5\u6570\u636e\u9a71\u52a8\u65b9\u5f0f\u7ba1\u7406\u53d1\u7248",
            members=[faces_store.member_key(fact)],
            confidence=0.9,
        )
        conn.execute("UPDATE schema_faces SET status='active' WHERE face_id=?", (face_id,))
        out = mcp_server._search(conn, query="\u53d1\u7248", top_k=5)
        by_id = {r["id"]: r for r in out["results"]}
        assert (
            by_id["e-fact"]["related_faces"][0]["signature"]
            == "\u7528\u6237\u4ee5\u6570\u636e\u9a71\u52a8\u65b9\u5f0f\u7ba1\u7406\u53d1\u7248"
        )
        assert "related_faces" not in by_id["e-plain"]


def test_related_faces_fail_open_without_schema_table(ac_root):
    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        _insert(conn, id="e-1", ts="2026-06-01T10:00", content="\u53d1\u7248 \u5907\u6ce8")
        out = mcp_server._search(conn, query="\u53d1\u7248", top_k=3)
        assert out["results"] and "related_faces" not in out["results"][0]


def test_bodies_excluded_by_default_included_by_param(ac_root, _roster_zhangwei):
    from persome.store import schema_faces as faces_store

    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        _insert(
            conn,
            id="e-schema",
            ts="2026-06-01T10:00",
            content="\u7528\u6237 \u60ef\u4e8e \u6570\u636e \u9a71\u52a8 \u51b3\u7b56",
            path="schema-user-profile.md",
        )
        body_id = faces_store.record_face(
            conn,
            source="emergent",
            signature="\u8de8\u57df\u878d\u5408\uff1a\u6570\u636e\u9a71\u52a8\u8d2f\u7a7f\u5f00\u53d1\u4e0e\u751f\u6d3b",
            members=["schema-user-profile.md", "schema-person-\u5f20\u4f1f.md"],
            level=2,
            confidence=0.8,
        )
        conn.execute("UPDATE schema_faces SET status='active' WHERE face_id=?", (body_id,))

        out = mcp_server._search(conn, query="\u6570\u636e \u9a71\u52a8", top_k=3)
        hit = next(r for r in out["results"] if r["id"] == "e-schema")
        assert "related_faces" not in hit

        out2 = mcp_server._search(
            conn, query="\u6570\u636e \u9a71\u52a8", top_k=3, include_bodies=True
        )
        hit2 = next(r for r in out2["results"] if r["id"] == "e-schema")
        assert hit2["related_faces"][0]["level"] == 2
        assert "\u8de8\u57df\u878d\u5408" in hit2["related_faces"][0]["signature"]


def test_level1_faces_carry_level_field(ac_root, _roster_zhangwei):
    from persome.store import schema_faces as faces_store

    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        fact = (
            "\u7528\u6237 \u575a\u6301 \u6570\u636e \u9a71\u52a8 \u7684 \u53d1\u7248 \u6d41\u7a0b"
        )
        _insert(conn, id="e-fact", ts="2026-06-01T10:00", content=fact)
        fid = faces_store.record_face(
            conn,
            source="mined",
            signature="\u7528\u6237\u4ee5\u6570\u636e\u9a71\u52a8\u65b9\u5f0f\u7ba1\u7406\u53d1\u7248",
            members=[faces_store.member_key(fact)],
        )
        conn.execute("UPDATE schema_faces SET status='active' WHERE face_id=?", (fid,))
        out = mcp_server._search(conn, query="\u53d1\u7248", top_k=3)
        hit = next(r for r in out["results"] if r["id"] == "e-fact")
        assert hit["related_faces"][0]["level"] == 1
