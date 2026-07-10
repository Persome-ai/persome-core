import pytest

from persome.evomem.models import MemoryLayer, MemoryNode, MemoryStatus
from persome.evomem.store import NodeStore


@pytest.fixture
def store(ac_root):  # ac_root 给临时 PERSOME_ROOT + index.db
    return NodeStore(user_id="u1")


def test_save_and_get(store):
    store.save(MemoryNode(node_id="a", content="喜欢咖啡", layer=MemoryLayer.L2_FACT))
    got = store.get("a")
    assert got is not None and got.content == "喜欢咖啡"
    assert got.is_latest is True


def test_search_returns_hits_with_node(store):
    store.save(MemoryNode(node_id="a", content="用户喜欢科幻电影", layer=MemoryLayer.L2_FACT))
    store.save(MemoryNode(node_id="b", content="用户住在上海", layer=MemoryLayer.L2_FACT))
    hits = store.search("科幻", top_k=5)
    assert any(h["node_id"] == "a" for h in hits)
    assert hits[0]["node"].content == "用户喜欢科幻电影"


def test_save_and_supersede_atomic_single_active_head(store):
    # issue #427：新链头落盘 + 旧节点 shadow 必须原子完成，结束后整条演化链
    # 只能有一个 is_latest=1 status=active 的活跃链头。
    store.save(MemoryNode(node_id="a", content="喝咖啡", layer=MemoryLayer.L2_FACT))
    new = MemoryNode(node_id="b", content="喝茶", layer=MemoryLayer.L2_FACT, supersedes=["a"])
    store.save_and_supersede(new, old_id="a")

    old, head = store.get("a"), store.get("b")
    assert old.status is MemoryStatus.SHADOW and old.is_latest is False
    assert old.superseded_by == ["b"]
    assert head.status is MemoryStatus.ACTIVE and head.is_latest is True
    assert head.supersedes == ["a"]
    # 不变量：恰好一个活跃链头。
    actives = [n for n in store.all_latest()]
    assert len(actives) == 1 and actives[0].node_id == "b"


def test_save_and_supersede_backfills_new_supersedes_pointer(store):
    # 即便调用方漏传 supersedes，方法也要兜底补上 old_id，保证链双向闭合。
    store.save(MemoryNode(node_id="a", content="喝咖啡", layer=MemoryLayer.L2_FACT))
    new = MemoryNode(node_id="b", content="喝茶", layer=MemoryLayer.L2_FACT)
    store.save_and_supersede(new, old_id="a")
    assert store.get("b").supersedes == ["a"]


def test_save_and_supersede_missing_old_raises(store):
    new = MemoryNode(node_id="b", content="喝茶", layer=MemoryLayer.L2_FACT)
    with pytest.raises(KeyError):
        store.save_and_supersede(new, old_id="does-not-exist")


def test_save_and_shadow_single_active_head_no_chain_link(store):
    # issue #448：UPDATE 落新链头 + shadow 旧节点必须原子，结束后只剩一个活跃链头；
    # 且 UPDATE 不进演化链——新旧节点都不带 supersede 双向指针。
    store.save(MemoryNode(node_id="a", content="喝咖啡", layer=MemoryLayer.L2_FACT))
    new = MemoryNode(node_id="b", content="喝美式咖啡", layer=MemoryLayer.L2_FACT)
    store.save_and_shadow(new, old_id="a")

    old, head = store.get("a"), store.get("b")
    assert old.status is MemoryStatus.SHADOW and old.is_latest is False
    assert old.superseded_by == []  # UPDATE 不建链：旧节点无 superseded_by 指针
    assert head.is_latest is True and head.status is MemoryStatus.ACTIVE
    assert head.supersedes == []  # 新节点也不 supersede 旧
    actives = store.all_latest()
    assert len(actives) == 1 and actives[0].node_id == "b"


def test_save_and_shadow_missing_old_still_saves_new(store):
    # 缺失的 old_id → UPDATE 命中 0 行（no-op），新节点仍照常落盘成唯一活跃链头。
    new = MemoryNode(node_id="b", content="x", layer=MemoryLayer.L2_FACT)
    store.save_and_shadow(new, old_id="does-not-exist")
    actives = store.all_latest()
    assert len(actives) == 1 and actives[0].node_id == "b"


def test_get_by_ids_returns_shadow_nodes_too(store):
    store.save(
        MemoryNode(
            node_id="a",
            content="x",
            layer=MemoryLayer.L2_FACT,
            status=MemoryStatus.SHADOW,
            is_latest=False,
        )
    )
    assert store.get_by_ids(["a"])[0].node_id == "a"
