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
    nodes = {"a": _mk("a", "\u5b64\u7acb\u4e8b\u5b9e")}
    hits = [{"node_id": "a", "score": 0.9, "node": nodes["a"]}]
    out = expand_evolution_chains(lambda ids: [nodes[i] for i in ids if i in nodes], hits)
    assert len(out) == 1 and "evolution_chain" not in out[0]


def test_two_node_chain_collapses_to_head_with_full_chain():
    a = _mk("a", "\u559d\u5496\u5561", superseded_by=["b"], is_latest=False)
    b = _mk("b", "\u559d\u8336", supersedes=["a"], is_latest=True)
    nodes = {"a": a, "b": b}

    hits = [{"node_id": "a", "score": 0.8, "node": a}]
    out = expand_evolution_chains(lambda ids: [nodes[i] for i in ids if i in nodes], hits)
    assert len(out) == 1
    assert out[0]["node_id"] == "b"
    assert out[0]["is_evolved"] is True
    chain = out[0]["evolution_chain"]
    assert [c["content"] for c in chain] == ["\u559d\u8336", "\u559d\u5496\u5561"]  # latest→oldest


def test_two_hits_same_chain_dedup_to_one_keeping_max_score():
    a = _mk("a", "\u559d\u5496\u5561", superseded_by=["b"], is_latest=False)
    b = _mk("b", "\u559d\u8336", supersedes=["a"], is_latest=True)
    nodes = {"a": a, "b": b}
    hits = [
        {"node_id": "a", "score": 0.5, "node": a},
        {"node_id": "b", "score": 0.9, "node": b},
    ]
    out = expand_evolution_chains(lambda ids: [nodes[i] for i in ids if i in nodes], hits)
    assert len(out) == 1
    assert out[0]["node_id"] == "b" and out[0]["score"] == 0.9
