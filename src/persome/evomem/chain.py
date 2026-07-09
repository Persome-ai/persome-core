"""演化链召回（``expand_evolution_chains``，teardown §5 同步移植）。

承重墙：把一批 search hits 折叠成"演化链"——命中链上任意节点都回溯到链头，
整条态度演变作为**一个**结果交给调用方，而不是抖出多条矛盾记忆。

与 Hy-Memory 原版的唯一差异：原版是 async，本重实现做同步版（persome 后台多为同步
调用，且更易测）。``get_by_ids`` 注入为同步 callable，便于测试直接传 dict-backed
lambda；engine 里传 ``store.get_by_ids``。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from .models import MemoryNode

GetByIds = Callable[[list[str]], list[MemoryNode]]

# memory_at/gmt_created 缺失时的兜底排序键（最旧），保证比较不抛 None。
_EPOCH = datetime.min


def _recency_key(node: MemoryNode) -> datetime:
    return node.memory_at or node.gmt_created or _EPOCH


def _trace_full_chain(get_by_ids: GetByIds, start: MemoryNode) -> list[MemoryNode]:
    """从链上任意节点双向 BFS 追溯整条链。

    向前追 ``supersedes`` 祖先、向后追 ``superseded_by`` 后继，直到没有新节点。
    返回链上全部节点（含 start），按 node_id 去重。
    """
    seen: dict[str, MemoryNode] = {start.node_id: start}
    frontier: list[str] = [*start.supersedes, *start.superseded_by]
    while frontier:
        wanted = [nid for nid in frontier if nid not in seen]
        if not wanted:
            break
        fetched = get_by_ids(wanted)
        frontier = []
        for node in fetched:
            if node.node_id in seen:
                continue
            seen[node.node_id] = node
            frontier.extend(node.supersedes)
            frontier.extend(node.superseded_by)
    return list(seen.values())


def _pick_head(chain: list[MemoryNode]) -> MemoryNode:
    """链头 = ``is_latest=True``；缺失则取 recency 最新；再退化取 node_id 稳定序。"""
    latest = [n for n in chain if n.is_latest]
    if latest:
        return max(latest, key=lambda n: (_recency_key(n), n.node_id))
    return max(chain, key=lambda n: (_recency_key(n), n.node_id))


def _order_latest_to_oldest(chain: list[MemoryNode]) -> list[MemoryNode]:
    return sorted(chain, key=lambda n: (_recency_key(n), n.node_id), reverse=True)


def _node_to_chain_item(node: MemoryNode) -> dict:
    return {
        "node_id": node.node_id,
        "content": node.content,
        "memory_at": node.memory_at,
        "gmt_created": node.gmt_created,
        "layer": node.layer,
    }


def expand_evolution_chains(get_by_ids: GetByIds, hits: list[dict]) -> list[dict]:
    """折叠 hits：在链上的命中回溯到链头并去重，孤立命中原样透传。

    ``hits`` 每项形如 ``{node_id, score, node}``。返回保持首次出现顺序的结果列表；
    链头项附加 ``is_evolved=True`` 与 ``evolution_chain``（latest→oldest）。
    """
    out: list[dict] = []
    covered: set[str] = set()  # 已被某条链覆盖的 node_id（跨 hit 去重）
    head_index: dict[str, int] = {}  # 链头 node_id → out 中的下标

    for hit in hits:
        node: MemoryNode = hit["node"]
        nid = node.node_id

        if not node.is_on_chain():
            if nid in covered:
                continue
            out.append({**hit})
            covered.add(nid)
            continue

        # 在链上：追溯整链 → 取链头代表。
        chain = _trace_full_chain(get_by_ids, node)
        head = _pick_head(chain)

        if head.node_id in head_index:
            # 同链已折叠过：保留最高 score，不重复追加。
            idx = head_index[head.node_id]
            out[idx]["score"] = max(out[idx]["score"], hit["score"])
            continue

        ordered = _order_latest_to_oldest(chain)
        result = {
            "node_id": head.node_id,
            "score": hit["score"],
            "node": head,
            "is_evolved": True,
            "evolution_chain": [_node_to_chain_item(n) for n in ordered],
        }
        head_index[head.node_id] = len(out)
        out.append(result)
        covered.update(n.node_id for n in chain)

    return out
