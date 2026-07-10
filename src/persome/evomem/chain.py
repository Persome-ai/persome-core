"Evolution-chain expansion and cycle-safe traversal."

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from .models import MemoryNode

GetByIds = Callable[[list[str]], list[MemoryNode]]


_EPOCH = datetime.min


def _recency_key(node: MemoryNode) -> datetime:
    return node.memory_at or node.gmt_created or _EPOCH


def _trace_full_chain(get_by_ids: GetByIds, start: MemoryNode) -> list[MemoryNode]:
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
    out: list[dict] = []
    covered: set[str] = set()
    head_index: dict[str, int] = {}

    for hit in hits:
        node: MemoryNode = hit["node"]
        nid = node.node_id

        if not node.is_on_chain():
            if nid in covered:
                continue
            out.append({**hit})
            covered.add(nid)
            continue

        chain = _trace_full_chain(get_by_ids, node)
        head = _pick_head(chain)

        if head.node_id in head_index:
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
