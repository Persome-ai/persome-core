"""向量/语义召回融合层（spec E3 / TODO #3，Phase 1）。

evomem 的图召回（``engine._recall`` → ``store.search``）一直是**子串-only**：整串 +
逐 token ``LIKE`` 命中活跃链头，跨措辞的同义召回（paraphrase）必漏。本模块在那条 LIKE
路径之上**叠加一路向量召回**，并用 RRF（Reciprocal Rank Fusion）把两路结果融合，再交给
``chain.expand_evolution_chains`` 折叠——LIKE 路径原样保留，向量路径只增不减。

设计纪律：

- **开关默认 off**（``cfg.evomem_vector_recall_enabled``，``getattr`` 兜底）：关时
  ``fuse`` 直接返回纯 LIKE 命中（``_lexical_hits`` 原样），行为与改动前**逐字节一致**，
  不回归。on 才走向量融合。
- **embedding 复用** ``intent.embeddings``（只读 import，不改它）：维度从该模块拿，
  **不写死**。冷启动 / 模型不可用（``embeddings.available()`` 为 False 或 ``embed`` 返回
  ``None``）→ 优雅回退到纯 LIKE，不抛、不告警、不变空。
- **融合用 RRF**（而非加权）：两路 score 量纲不同（LIKE 是 ``1.0 - i/len`` 的位置分，
  cosine 是 [−1,1] 相似度），RRF 只看**排名**，对量纲不敏感，是混合检索的稳健默认。
  MMR / 时间衰减是后续（Phase 2）的二次重排，**接口上留好钩子**（``fuse`` 返回融合后的
  ``[{node_id, score, node}]``，后续 rerank 可在其上叠加，不堵死）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from ..intent import embeddings

if TYPE_CHECKING:
    from .models import MemoryNode

# RRF 常数（Cormack et al. 2009 经典默认）：rank 从 0 起，贡献 1/(k + rank + 1)。
# k 越大，靠前名次之间的差距越平缓；60 是检索社区的稳健默认。
_RRF_K = 60

# 向量候选默认取的链头条数上限。向量召回要对**全部活跃链头**算 cosine，小库够用；
# 这是单次召回 embed 的节点数上限，避免大库时 O(N) embed 失控。
_VECTOR_CANDIDATE_LIMIT = 200


def _embed_text(node: MemoryNode) -> str:
    """节点参与向量召回的文本：``content`` 为主，``schema_summary`` / ``tags`` 补充。

    evomem 的 ``MemoryNode`` 没有独立 ``title`` 列（语义都在 ``content``），故以
    ``content`` 为主体；schema 节点的摘要、语义 tag 作为附加上下文拼进去（都为空则
    只剩 content）。
    """
    parts = [node.content or ""]
    summary = getattr(node, "schema_summary", None)
    if summary:
        parts.append(summary)
    tags = getattr(node, "tags", "") or ""
    if tags:
        parts.append(tags)
    return "\n".join(p for p in parts if p)


def _flag_on(cfg: object) -> bool:
    """开关位（``getattr`` 兜底，纯布尔，不看模型可用性）。

    config.py 当前没有该字段（按任务约定不改它），``getattr(..., False)`` 让本层在
    字段加进来之前默认关。两种承载都接受：

    - 扁平属性 ``cfg.evomem_vector_recall_enabled``（任务给定的兜底键，也便于测试直接
      传 ``SimpleNamespace(evomem_vector_recall_enabled=True)``）；
    - 嵌套 ``cfg.evomem.vector_recall_enabled``（真实 ``Config`` 落字段后的位置——我之后
      在 ``EvomemConfig`` 加 ``vector_recall_enabled``，本层无需再改）。
    """
    if getattr(cfg, "evomem_vector_recall_enabled", False):
        return True
    evomem = getattr(cfg, "evomem", None)
    return bool(getattr(evomem, "vector_recall_enabled", False))


def vector_enabled(cfg: object) -> bool:
    """开关解析：开关位为真 **且** embedding 模型可用，才走向量融合。

    ``embeddings.available()`` 再叠一道：模型/runtime 不在时即便开关开也回退 LIKE
    （冷启动优雅退化）。
    """
    return _flag_on(cfg) and embeddings.available()


def _rank_by_cosine(
    query: str,
    candidates: Sequence[MemoryNode],
    *,
    top_k: int,
) -> list[dict]:
    """对候选链头按与 query 的 cosine 排序，返回 ``[{node_id, score, node}]``。

    维度/向量从 ``embeddings`` 拿（不写死）；query 或候选 embed 不出来（``None``）的
    跳过——整条 query embed 不出来时返回空列表，由 ``fuse`` 回退纯 LIKE。
    """
    q_vec = embeddings.embed(query)
    if q_vec is None:
        return []
    scored: list[dict] = []
    for node in candidates:
        n_vec = embeddings.embed(_embed_text(node))
        if n_vec is None:
            continue
        sim = embeddings.cosine(q_vec, n_vec)
        scored.append({"node_id": node.node_id, "score": float(sim), "node": node})
    scored.sort(key=lambda h: h["score"], reverse=True)
    return scored[:top_k]


def _rrf_merge(*ranked_lists: list[dict], top_k: int) -> list[dict]:
    """Reciprocal Rank Fusion：按各路**排名**累加 1/(k + rank + 1)，同节点取并集。

    每路输入是已按自身 score 降序的 ``[{node_id, score, node}]``。融合后的 ``score``
    是 RRF 分（非原始 score），用于跨路统一排序；``node`` 取首次见到的实例。
    """
    fused: dict[str, dict] = {}
    for ranked in ranked_lists:
        for rank, hit in enumerate(ranked):
            nid = hit["node_id"]
            contrib = 1.0 / (_RRF_K + rank + 1)
            slot = fused.get(nid)
            if slot is None:
                fused[nid] = {
                    "node_id": nid,
                    "score": contrib,
                    "node": hit["node"],
                }
            else:
                slot["score"] += contrib
    out = sorted(fused.values(), key=lambda h: h["score"], reverse=True)
    return out[:top_k]


def fuse(
    query: str,
    lexical_hits: list[dict],
    *,
    top_k: int,
    cfg: object,
    candidates_provider: Callable[[], Sequence[MemoryNode]],
) -> list[dict]:
    """融合入口：开关 off / 模型不可用 → 原样返回 LIKE 命中；on → LIKE ⊕ 向量 RRF 融合。

    参数：
    - ``lexical_hits``：现有 LIKE 路径（``engine._recall``）的命中，``[{node_id, score, node}]``。
    - ``candidates_provider``：惰性提供向量召回的候选链头集（活跃链头），只有开关开 +
      模型可用时才调用（关时零成本，不 embed、不查库）。

    回退语义（任一即纯 LIKE）：开关 off、``embeddings.available()`` 为 False、query
    embed 不出来。这些都返回 ``lexical_hits`` 原样（截断到 ``top_k`` 不改其相对序），
    保证「开关 off / 冷启动」与改动前一致。
    """
    if not vector_enabled(cfg):
        return lexical_hits[:top_k]

    candidates = list(candidates_provider())[:_VECTOR_CANDIDATE_LIMIT]
    vector_hits = _rank_by_cosine(query, candidates, top_k=top_k)
    if not vector_hits:
        # 模型在但 query/候选 embed 不出来：优雅回退纯 LIKE。
        return lexical_hits[:top_k]

    return _rrf_merge(lexical_hits, vector_hits, top_k=top_k)
