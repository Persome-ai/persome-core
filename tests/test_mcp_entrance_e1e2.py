"""MCP 满血入口 E1+E2（spec 2026-07-06-mcp-full-power-memory-entrance-design.md）.

E1.1 breadth：§3.4-3 的消费端广度旋钮（MMR）终于能从 MCP 调用方传进来——含无槽
退化路；0 = 字节等价。E1.2 entities：显式 Who 槽，走与蒸馏 Q 同一把
``resolve_identity``（§4.3 单码本红线）。E1.3 read_receipt：``⟨entry_id:path⟩``
收据把手的解引用器（渐进披露一跳下钻 + capture breadcrumbs）。E2 entity_graph：
图层直读（谓词边 + as-of 时travel + 到 USER 链 + shadow 单列）。零 LLM、零网络。
"""

from __future__ import annotations

import pytest

from persome.evomem import identity as identity_mod
from persome.mcp import server as mcp_server
from persome.retrieval import associative as assoc_mod
from persome.store import fts
from persome.store import relation_edges as edges_store


@pytest.fixture()
def _roster_zhangwei(monkeypatch):
    """A roster that knows 张伟 — patched into the ONE identity funnel."""
    roster = identity_mod.Roster.build([("张伟", ["伟哥"]), ("self", [])])
    monkeypatch.setattr(identity_mod, "load_roster", lambda cfg, **kw: roster)
    return roster


def _insert(conn, *, id: str, ts: str, content: str, path: str = "topic-x.md"):
    conn.execute(
        "INSERT INTO entries (id, path, prefix, timestamp, tags, content, superseded)"
        " VALUES (?, ?, 'topic', ?, '', ?, 0)",
        (id, path, ts, content),
    )


# ── E1.1 breadth（消费端广度旋钮） ────────────────────────────────────────────


def test_breadth_trades_redundancy_for_coverage(ac_root):
    """三条近重复 + 一条异质；breadth=0 → 近重复霸榜（准度优先字节等价）；
    breadth>0 → 异质条目进 top-2（只有旋钮能答的桶）。"""
    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        _insert(conn, id="e-dup1", ts="2026-06-01T10:00", content="部署 流程 检查单 步骤 一")
        _insert(conn, id="e-dup2", ts="2026-06-01T10:01", content="部署 流程 检查单 步骤 二")
        _insert(conn, id="e-dup3", ts="2026-06-01T10:02", content="部署 流程 检查单 步骤 三")
        _insert(conn, id="e-alt", ts="2026-06-01T10:03", content="部署 回滚 预案 完全 不同 内容")
        narrow = [h.id for h in fts.search_hybrid(conn, query="部署 流程", top_k=2)]
        assert "e-alt" not in narrow  # 近重复更贴 query，准度优先
        wide = [
            h.id for h in fts.search_hybrid(conn, query="部署 流程", top_k=2, mmr_diversity=0.8)
        ]
        assert "e-alt" in wide  # 广度换覆盖
        # membership 纪律：旋钮只改序/选择，不发明候选
        assert set(wide) <= {"e-dup1", "e-dup2", "e-dup3", "e-alt"}


def test_breadth_survives_slotless_degrade_via_associative_read(ac_root, _roster_zhangwei):
    """associative_read 无槽退化到 search_hybrid 时旋钮不失效（E1.1 的存在理由）。"""
    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        _insert(conn, id="e-dup1", ts="2026-06-01T10:00", content="部署 流程 检查单 步骤 一")
        _insert(conn, id="e-dup2", ts="2026-06-01T10:01", content="部署 流程 检查单 步骤 二")
        _insert(conn, id="e-alt", ts="2026-06-01T10:02", content="部署 回滚 预案 完全 不同 内容")
        hits = assoc_mod.associative_read(conn, query="部署 流程", top_k=2, mmr_diversity=0.8)
        assert "e-alt" in [h.id for h in hits]


# ── E1.2 explicit entities（显式 Who 槽·同一漏斗） ────────────────────────────


def test_explicit_entities_arm_the_who_head(ac_root, _roster_zhangwei):
    """query 文本完全不含人名；entities=['伟哥']（别名）经 resolve_identity 消解为
    张伟并武装实体头——文本头到不了的条目被实体头捞回。"""
    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        _insert(conn, id="e-zw", ts="2026-06-01T10:00", content="张伟 负责 发版 事宜")
        _insert(conn, id="e-noise", ts="2026-06-01T10:01", content="完全 无关 的 内容")
        hits = assoc_mod.associative_read(conn, query="那件事谁在跟", top_k=3, entities=["伟哥"])
        assert "e-zw" in [h.id for h in hits]
        # 未知名字静默丢弃（增强不是过滤），读路不炸
        hits2 = assoc_mod.associative_read(
            conn, query="那件事谁在跟", top_k=3, entities=["不存在的人"]
        )
        assert isinstance(hits2, list)


# ── E1.3 read_receipt（收据把手） ─────────────────────────────────────────────


def test_read_receipt_dereferences_and_breadcrumbs(ac_root):
    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        _insert(conn, id="e-r1", ts="2026-06-01T10:00:00", content="发版 0.3.9 已出包")
        conn.execute(
            "INSERT INTO captures (id, timestamp, app_name, window_title, visible_text)"
            " VALUES ('cap-1', '2026-06-01T10:05:00', 'Feishu', '发版群', '出包了')"
        )
        out = mcp_server._read_receipt(conn, entry_id="e-r1")
        assert out["id"] == "e-r1" and out["superseded"] is False
        assert out["content"].startswith("发版")
        assert isinstance(out["age_days"], int)
        assert [c["id"] for c in out["nearby_captures"]] == ["cap-1"]
        # 读即强化
        assert fts.get_retrieval_count(conn, "e-r1") == 1


def test_read_receipt_honest_miss_and_superseded_label(ac_root):
    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        assert "error" in mcp_server._read_receipt(conn, entry_id="nope")
        conn.execute(
            "INSERT INTO entries (id, path, prefix, timestamp, tags, content, superseded)"
            " VALUES ('e-old', 'topic-x.md', 'topic', '2026-05-01T10:00', '', '旧版本 事实', 1)"
        )
        out = mcp_server._read_receipt(conn, entry_id="e-old")
        assert out["superseded"] is True  # 考古可读，但明确标注
        assert fts.get_retrieval_count(conn, "e-old") == 0  # superseded 不强化


# ── E2 entity_graph（图与时间） ───────────────────────────────────────────────


def _seed_edges(conn):
    edges_store.ensure_schema(conn)
    edges_store.add_edge(
        conn,
        src_identity="self",
        dst_identity="张伟",
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
        src_identity="张伟",
        dst_identity="李老板",
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
        src_identity="张伟",
        dst_identity="神秘人",
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
        out = mcp_server._entity_graph(conn, None, name="伟哥", depth=2)
        assert out["resolved"] == "张伟"
        preds = {e["predicate"] for e in out["edges"]}
        assert preds == {"knows", "reports_to"}  # ACTIVE only
        assert "李老板" in out["neighbors"] and "神秘人" not in out["neighbors"]
        assert out["chain_to_user"] and "张伟" in out["chain_to_user"]
        with_shadow = mcp_server._entity_graph(
            conn, None, name="张伟", depth=2, include_shadow=True
        )
        assert any(e["dst"] == "神秘人" for e in with_shadow["shadow_edges"])
        assert "神秘人" in with_shadow["neighbors"]


def test_entity_graph_as_of_time_travel(ac_root, _roster_zhangwei):
    """as_of 早于边的 valid_from → 那时还不存在这段关系（bitemporal 一等操作）。"""
    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        _seed_edges(conn)
        out = mcp_server._entity_graph(conn, None, name="张伟", as_of="2026-04-01T00:00")
        assert out["edges"] == []
        out2 = mcp_server._entity_graph(conn, None, name="张伟", as_of="2026-06-01T00:00")
        assert len(out2["edges"]) == 2


def test_entity_graph_honest_miss_for_unknown_name(ac_root, _roster_zhangwei):
    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        out = mcp_server._entity_graph(conn, None, name="完全陌生的名字")
        assert out["resolved"] is None
        assert "layer" in out and "note" in out


# ── E1.5 related_faces（recall↔schema 关联） ──────────────────────────────────


def test_search_hits_carry_covering_faces(ac_root, _roster_zhangwei):
    """命中被某个转正 face 的足迹覆盖（member_key 隶属）→ 结果附 related_faces
    （事实 + 解释它的行为规律）；未覆盖的命中不带该字段（不制造噪声）。"""
    from persome.store import schema_faces as faces_store

    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        fact = "用户 坚持 数据 驱动 的 发版 流程"
        _insert(conn, id="e-fact", ts="2026-06-01T10:00", content=fact)
        _insert(conn, id="e-plain", ts="2026-06-01T10:01", content="发版 无关 备注")
        face_id = faces_store.record_face(
            conn,
            source="mined",
            signature="用户以数据驱动方式管理发版",
            members=[faces_store.member_key(fact)],
            confidence=0.9,
        )
        conn.execute("UPDATE schema_faces SET status='active' WHERE face_id=?", (face_id,))
        out = mcp_server._search(conn, query="发版", top_k=5)
        by_id = {r["id"]: r for r in out["results"]}
        assert by_id["e-fact"]["related_faces"][0]["signature"] == "用户以数据驱动方式管理发版"
        assert "related_faces" not in by_id["e-plain"]


def test_related_faces_fail_open_without_schema_table(ac_root):
    """老库没有 schema_faces 也绝不炸——关联是装饰，不是依赖。"""
    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        _insert(conn, id="e-1", ts="2026-06-01T10:00", content="发版 备注")
        out = mcp_server._search(conn, query="发版", top_k=3)
        assert out["results"] and "related_faces" not in out["results"][0]


def test_bodies_excluded_by_default_included_by_param(ac_root, _roster_zhangwei):
    """体（level-2）默认不返回；include_bodies=True 时按路径隶属附上（体的
    members 是 schema 文件名——面→体无存储指针，路径隶属是诚实的确定性覆盖）。"""
    from persome.store import schema_faces as faces_store

    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        _insert(
            conn,
            id="e-schema",
            ts="2026-06-01T10:00",
            content="用户 惯于 数据 驱动 决策",
            path="schema-user-profile.md",
        )
        body_id = faces_store.record_face(
            conn,
            source="emergent",
            signature="跨域融合：数据驱动贯穿开发与生活",
            members=["schema-user-profile.md", "schema-person-张伟.md"],
            level=2,
            confidence=0.8,
        )
        conn.execute("UPDATE schema_faces SET status='active' WHERE face_id=?", (body_id,))
        # 默认：体不出现
        out = mcp_server._search(conn, query="数据 驱动", top_k=3)
        hit = next(r for r in out["results"] if r["id"] == "e-schema")
        assert "related_faces" not in hit
        # include_bodies=True：体按路径隶属附上，带 level=2 标注
        out2 = mcp_server._search(conn, query="数据 驱动", top_k=3, include_bodies=True)
        hit2 = next(r for r in out2["results"] if r["id"] == "e-schema")
        assert hit2["related_faces"][0]["level"] == 2
        assert "跨域融合" in hit2["related_faces"][0]["signature"]


def test_level1_faces_carry_level_field(ac_root, _roster_zhangwei):
    from persome.store import schema_faces as faces_store

    with fts.cursor() as conn:
        conn.executescript(fts.SCHEMA)
        fact = "用户 坚持 数据 驱动 的 发版 流程"
        _insert(conn, id="e-fact", ts="2026-06-01T10:00", content=fact)
        fid = faces_store.record_face(
            conn,
            source="mined",
            signature="用户以数据驱动方式管理发版",
            members=[faces_store.member_key(fact)],
        )
        conn.execute("UPDATE schema_faces SET status='active' WHERE face_id=?", (fid,))
        out = mcp_server._search(conn, query="发版", top_k=3)
        hit = next(r for r in out["results"] if r["id"] == "e-fact")
        assert hit["related_faces"][0]["level"] == 1
