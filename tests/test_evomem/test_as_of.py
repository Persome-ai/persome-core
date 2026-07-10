"Tests for test as of."

from __future__ import annotations

from datetime import datetime

from persome.evomem.as_of import node_as_of, nodes_as_of
from persome.evomem.models import MemoryLayer, MemoryNode, MemoryStatus
from persome.evomem.store import NodeStore
from persome.store import fts


def _node(nid: str, content: str, *, created: str, **kw) -> MemoryNode:
    return MemoryNode(
        node_id=nid,
        content=content,
        layer=MemoryLayer.L2_FACT,
        gmt_created=datetime.fromisoformat(created),
        file_name=kw.pop("file_name", "person-\u5f20\u4f1f.md"),
        **kw,
    )


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _conn(ac_root):
    NodeStore()  # ensure schema
    return fts.cursor()


class TestNodesAsOf:
    def test_replays_transaction_clock(self, ac_root):
        store = NodeStore()
        store.save(
            _node(
                "a",
                "\u5f20\u4f1f\u662f\u540e\u7aef\u5de5\u7a0b\u5e08",
                created="2026-02-01T10:00:00",
            )
        )
        new = _node(
            "b",
            "\u5f20\u4f1f\u662f\u540e\u7aef\u8d1f\u8d23\u4eba",
            created="2026-04-01T10:00:00",
            supersedes=["a"],
        )
        store.save_and_supersede(new, old_id="a")
        with _conn(ac_root) as conn:
            # March: only the old version existed un-superseded
            march = nodes_as_of(
                conn, file_name="person-\u5f20\u4f1f.md", t=_t("2026-03-15T00:00:00")
            )
            assert [n.node_id for n in march] == ["a"]
            # May: the successor has replaced it
            may = nodes_as_of(conn, file_name="person-\u5f20\u4f1f.md", t=_t("2026-05-01T00:00:00"))
            assert [n.node_id for n in may] == ["b"]
            # January: nothing written yet
            assert (
                nodes_as_of(conn, file_name="person-\u5f20\u4f1f.md", t=_t("2026-01-01T00:00:00"))
                == []
            )

    def test_validity_window_filters_when_present(self, ac_root):
        store = NodeStore()
        store.save(
            _node(
                "boss-q1",
                "\u5f20\u4f1f\u7684\u8001\u677f\u662f Lily",
                created="2026-01-05T00:00:00",
                valid_from="2026-01-01T00:00:00",
                valid_until="2026-03-31T23:59:00",
            )
        )
        store.save(
            _node(
                "boss-q2",
                "\u5f20\u4f1f\u7684\u8001\u677f\u662f Bob",
                created="2026-01-05T00:00:00",
                valid_from="2026-04-01T00:00:00",
            )
        )
        with _conn(ac_root) as conn:
            march = nodes_as_of(
                conn, file_name="person-\u5f20\u4f1f.md", t=_t("2026-03-01T00:00:00")
            )
            assert [n.node_id for n in march] == ["boss-q1"]
            june = nodes_as_of(
                conn, file_name="person-\u5f20\u4f1f.md", t=_t("2026-06-01T00:00:00")
            )
            assert [n.node_id for n in june] == ["boss-q2"]

    def test_unwindowed_nodes_pass_fail_open(self, ac_root):
        store = NodeStore()
        store.save(_node("a", "\u65e0\u7a97\u53e3\u4e8b\u5b9e", created="2026-02-01T00:00:00"))
        with _conn(ac_root) as conn:
            got = nodes_as_of(conn, file_name="person-\u5f20\u4f1f.md", t=_t("2026-12-01T00:00:00"))
        assert [n.node_id for n in got] == ["a"]

    def test_mixed_timezone_never_explodes(self, ac_root):
        store = NodeStore()
        store.save(_node("a", "aware \u5199\u5165", created="2026-02-01T10:00:00+08:00"))
        with _conn(ac_root) as conn:
            got = nodes_as_of(conn, file_name="person-\u5f20\u4f1f.md", t=_t("2026-03-01T00:00:00"))
        assert [n.node_id for n in got] == ["a"]

    def test_scope_isolation(self, ac_root):
        NodeStore(user_id="u1").save(
            _node("a", "u1 \u7684\u4e8b\u5b9e", created="2026-02-01T00:00:00")
        )
        with _conn(ac_root) as conn:
            assert (
                nodes_as_of(conn, file_name="person-\u5f20\u4f1f.md", t=_t("2026-03-01T00:00:00"))
                == []
            )
            got = nodes_as_of(
                conn, file_name="person-\u5f20\u4f1f.md", t=_t("2026-03-01T00:00:00"), user_id="u1"
            )
        assert [n.node_id for n in got] == ["a"]

    def test_unknown_identity_is_empty(self, ac_root):
        with _conn(ac_root) as conn:
            assert (
                nodes_as_of(conn, file_name="person-nobody.md", t=_t("2026-01-01T00:00:00")) == []
            )


class TestNodeAsOf:
    def test_resolves_chain_version_from_any_id(self, ac_root):
        store = NodeStore()
        store.save(_node("a", "v1", created="2026-02-01T00:00:00"))
        store.save_and_supersede(
            _node("b", "v2", created="2026-04-01T00:00:00", supersedes=["a"]), old_id="a"
        )
        store.save_and_supersede(
            _node("c", "v3", created="2026-06-01T00:00:00", supersedes=["b"]), old_id="b"
        )
        with _conn(ac_root) as conn:
            # asking from the NEWEST id about March lands on v1
            got = node_as_of(conn, node_id="c", t=_t("2026-03-01T00:00:00"))
            assert got is not None and got.node_id == "a"
            # asking from the OLDEST id about May lands on v2
            got = node_as_of(conn, node_id="a", t=_t("2026-05-01T00:00:00"))
            assert got is not None and got.node_id == "b"
            # before anything existed → None
            assert node_as_of(conn, node_id="c", t=_t("2026-01-01T00:00:00")) is None

    def test_unknown_id_is_none(self, ac_root):
        with _conn(ac_root) as conn:
            assert node_as_of(conn, node_id="nope", t=_t("2026-01-01T00:00:00")) is None

    def test_dangling_successor_pointer_keeps_row(self, ac_root):
        store = NodeStore()
        store.save(
            _node(
                "a", "v1", created="2026-02-01T00:00:00", superseded_by=["ghost"], is_latest=False
            )
        )
        with _conn(ac_root) as conn:
            got = node_as_of(conn, node_id="a", t=_t("2026-03-01T00:00:00"))
        assert got is not None and got.node_id == "a"  # can't date the supersede — keep

    def test_shadowed_node_respects_valid_until(self, ac_root):
        store = NodeStore()
        store.save(_node("a", "v1", created="2026-02-01T00:00:00"))
        store.shadow("a", valid_until="2026-05-01T00:00:00")
        with _conn(ac_root) as conn:
            before = node_as_of(conn, node_id="a", t=_t("2026-03-01T00:00:00"))
            assert before is not None and before.status == MemoryStatus.SHADOW  # current status
            after = node_as_of(conn, node_id="a", t=_t("2026-06-01T00:00:00"))
        assert after is None  # validity window closed before T
