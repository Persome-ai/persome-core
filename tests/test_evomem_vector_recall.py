"""E3 / TODO #3 (Phase 1) — evomem 图召回的向量/语义融合层。

验收(spec E3):
- 开关 off(默认):召回与改动前一致(纯 LIKE 子串)——一个换措辞、子串匹配不到的
  查询召回不到目标节点。
- 开关 on + embedding 可用:同一个 paraphrase 查询能召回到语义相同的节点(向量路径
  补上 LIKE 漏的同义召回)。
- embedding 不可用时优雅回退纯 LIKE,不抛、不崩。
- 维度不写死(用 2 维假向量即可跑通,证明未绑定具体维度)。

embedding 走依赖注入式 monkeypatch(`persome.intent.embeddings`),不依赖真实模型/网络。
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from persome.evomem.engine import EvoMemory
from persome.evomem.models import MemoryLayer

# 两条互不重叠的语义:运动 / 部署。query 与目标无任何子串/单字重叠,故 LIKE 必漏,
# 只有向量路径能召回。
_RUN_NODE = "user enjoys jogging workouts"
_DEPLOY_NODE = "deploy the service to production"
_PARAPHRASE_QUERY = "morning exercise routine"  # 与 _RUN_NODE 零词面重叠

_EXERCISE_KEYS = ("jogging", "workout", "exercise", "morning", "routine", "run")
_DEPLOY_KEYS = ("deploy", "production", "service", "release")


def _fake_embed(text: str) -> np.ndarray | None:
    """2 维概念向量:运动轴 / 部署轴。无关文本落中性向量。"""
    t = (text or "").lower()
    if any(k in t for k in _EXERCISE_KEYS):
        return np.array([1.0, 0.0])
    if any(k in t for k in _DEPLOY_KEYS):
        return np.array([0.0, 1.0])
    return np.array([0.5, 0.5])


@pytest.fixture
def _embeddings_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("persome.intent.embeddings.available", lambda: True)
    monkeypatch.setattr("persome.intent.embeddings.embed", _fake_embed)
    # cosine 用真实实现(对 numpy 向量直接算)。


def _seed(mem: EvoMemory) -> None:
    mem.add_direct(_RUN_NODE, layer=MemoryLayer.L2_FACT, file_name="user-habits.md")
    mem.add_direct(_DEPLOY_NODE, layer=MemoryLayer.L2_FACT, file_name="project-x.md")


def _contents(hits: list[dict]) -> list[str]:
    return [h["node"].content for h in hits]


def test_flag_off_is_lexical_only(ac_root) -> None:
    """默认(无开关):paraphrase 查询召回不到运动节点——纯 LIKE 行为。"""
    mem = EvoMemory(cfg=SimpleNamespace())  # 无 evomem_vector_recall_enabled → 关
    _seed(mem)
    hits = mem.search(_PARAPHRASE_QUERY, top_k=10)
    assert _RUN_NODE not in _contents(hits)


def test_flag_on_recalls_paraphrase(ac_root, _embeddings_on) -> None:
    """开关 on + embedding 可用:paraphrase 查询经向量路径召回到运动节点。"""
    mem = EvoMemory(cfg=SimpleNamespace(evomem_vector_recall_enabled=True))
    _seed(mem)
    hits = mem.search(_PARAPHRASE_QUERY, top_k=10)
    contents = _contents(hits)
    assert _RUN_NODE in contents  # 向量补回了 LIKE 漏的同义召回
    # RRF 在 top_k≥库容时会返回全部节点(按融合分排序),故不断言部署节点缺席,
    # 而断言语义匹配的运动节点排在最前(正交的部署节点排其后)。
    assert contents[0] == _RUN_NODE
    assert contents.index(_RUN_NODE) < contents.index(_DEPLOY_NODE)


def test_flag_on_but_embeddings_unavailable_falls_back(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """开关 on 但模型不可用 → 优雅回退纯 LIKE(与 off 一致),不抛。"""
    monkeypatch.setattr("persome.intent.embeddings.available", lambda: False)
    mem = EvoMemory(cfg=SimpleNamespace(evomem_vector_recall_enabled=True))
    _seed(mem)
    hits = mem.search(_PARAPHRASE_QUERY, top_k=10)
    assert _RUN_NODE not in _contents(hits)  # 回退后召回不到 = 纯 LIKE


def test_lexical_hit_preserved_under_fusion(ac_root, _embeddings_on) -> None:
    """开关 on 时,能子串命中的查询仍召回(LIKE ⊕ 向量融合,不丢 LIKE 命中)。"""
    mem = EvoMemory(cfg=SimpleNamespace(evomem_vector_recall_enabled=True))
    _seed(mem)
    hits = mem.search("jogging", top_k=10)  # 直接子串命中运动节点
    assert _RUN_NODE in _contents(hits)


def test_nested_config_flag_also_honored(ac_root, _embeddings_on) -> None:
    """嵌套承载 cfg.evomem.vector_recall_enabled 也能打开(真实 Config 落字段后的位置)。"""
    cfg = SimpleNamespace(evomem=SimpleNamespace(vector_recall_enabled=True))
    mem = EvoMemory(cfg=cfg)
    _seed(mem)
    assert _RUN_NODE in _contents(mem.search(_PARAPHRASE_QUERY, top_k=10))
