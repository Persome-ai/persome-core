from persome.evomem.chain import expand_evolution_chains
from persome.evomem.models import MemoryLayer, MemoryNode


def _mk(nid, content, supersedes=None, superseded_by=None, is_latest=True):
    return MemoryNode(
        node_id=nid,
        content=content,
        layer=MemoryLayer.L2_FACT,
        supersedes=supersedes or [],
        superseded_by=superseded_by or [],
        is_latest=is_latest,
    )


def test_no_chain_passthrough():
    nodes = {"a": _mk("a", "孤立事实")}
    hits = [{"node_id": "a", "score": 0.9, "node": nodes["a"]}]
    out = expand_evolution_chains(lambda ids: [nodes[i] for i in ids if i in nodes], hits)
    assert len(out) == 1 and "evolution_chain" not in out[0]


def test_two_node_chain_collapses_to_head_with_full_chain():
    a = _mk("a", "喝咖啡", superseded_by=["b"], is_latest=False)
    b = _mk("b", "喝茶", supersedes=["a"], is_latest=True)
    nodes = {"a": a, "b": b}
    # 命中的是旧节点 a，应回溯到链头 b
    hits = [{"node_id": "a", "score": 0.8, "node": a}]
    out = expand_evolution_chains(lambda ids: [nodes[i] for i in ids if i in nodes], hits)
    assert len(out) == 1
    assert out[0]["node_id"] == "b"  # 代表节点 = 链头
    assert out[0]["is_evolved"] is True
    chain = out[0]["evolution_chain"]
    assert [c["content"] for c in chain] == ["喝茶", "喝咖啡"]  # latest→oldest


def test_two_hits_same_chain_dedup_to_one_keeping_max_score():
    a = _mk("a", "喝咖啡", superseded_by=["b"], is_latest=False)
    b = _mk("b", "喝茶", supersedes=["a"], is_latest=True)
    nodes = {"a": a, "b": b}
    hits = [
        {"node_id": "a", "score": 0.5, "node": a},
        {"node_id": "b", "score": 0.9, "node": b},
    ]
    out = expand_evolution_chains(lambda ids: [nodes[i] for i in ids if i in nodes], hits)
    assert len(out) == 1
    assert out[0]["node_id"] == "b" and out[0]["score"] == 0.9
