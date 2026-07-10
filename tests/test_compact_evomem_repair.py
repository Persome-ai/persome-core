"""issue #526 回归：切主读后 compact 致 evo_nodes 丢记忆 → 自动 repair 自修。

链条（复现）：
- compact（markdown 主写模式）LLM 整文件重写 + ``rebuild_index`` 绕过三条写路，
  给条目换新 id；
- 旧行为只 ``note_out_of_band_rewrite`` 记 alert-only miss，daemon 无自动修复；
- production associative recall and the evo_nodes authority must agree after
  compact changes entry ids.

修复：``run_pending`` accept 后同步调用 ``restore.import_from_markdown``，从 markdown
SSOT 整库重建 evo_nodes（清掉换 id 留下的孤儿 head），折叠 recall 当场恢复。
"""

from __future__ import annotations

import re

from persome import config as config_mod
from persome.evomem import backfill
from persome.retrieval import associative as recall
from persome.store import entries as entries_mod
from persome.store import files as files_mod
from persome.store import fts
from persome.writer import compact as compact_mod
from persome.writer import llm as llm_mod

_ID_RE = re.compile(r"\{id:\s*([0-9a-zA-Z-]+)\}")

_HINT = "widgetcorpus"
_BODY = (
    "用户在做 widget 项目 widgetcorpus alpha beta gamma delta epsilon zeta eta theta "
    "iota kappa lambda mu nu xi omicron pi rho sigma tau upsilon phi chi psi omega"
)


def _rewrite_with_new_ids(markdown: str) -> str:
    """模拟 compact LLM：保留全部正文 token（过 95% 保留闸），但给每个条目换新 id
    ——真实 compact 重写整文件时模型重新生成 ``{id: ...}`` 标记，原 id 不再出现。"""
    counter = {"n": 0}

    def _sub(_m: re.Match[str]) -> str:
        counter["n"] += 1
        return f"{{id: 99999999-9999-{counter['n']:06d}}}"

    return _ID_RE.sub(_sub, markdown)


def _seed_and_backfill() -> None:
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-x.md", description="x", tags=["t"])
        entries_mod.append_entry(conn, name="project-x.md", content=_BODY, tags=["t"])
    report = backfill.run_backfill()
    assert report.ok


def _production_recall() -> str:
    with fts.cursor() as conn:
        hits = recall.associative_read(conn, query=_HINT, top_k=10)
        return "\n".join(hit.content for hit in hits)


def _evo_node_ids() -> set[str]:
    with fts.cursor() as conn:
        return {
            r["node_id"] for r in conn.execute("SELECT node_id FROM evo_nodes WHERE is_latest = 1")
        }


def _run_compact_with_id_churn(monkeypatch) -> None:
    """跑一次会换 id 的 compact（accept）。"""
    original = files_mod.memory_path("project-x.md").read_text()
    rewritten = _rewrite_with_new_ids(original)
    assert "{id: 99999999" in rewritten and original != rewritten
    monkeypatch.setattr(llm_mod, "call_llm", lambda *a, **k: object())
    monkeypatch.setattr(llm_mod, "extract_text", lambda resp: rewritten)
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        fts.set_needs_compact(conn, "project-x.md", True)
        results = compact_mod.run_pending(cfg, conn)
    assert any(r.accepted for r in results), "compact 应被接受（保留率 100%）"


def test_compact_repairs_evomem_synchronously(ac_root, monkeypatch) -> None:
    """compact 换 id 后同步从 markdown 重建 evo_nodes，折叠 recall 不留坏窗口。"""
    _seed_and_backfill()
    old_ids = _evo_node_ids()
    assert "widget 项目" in _production_recall(), "backfill 后 production recall 应能看到记忆"

    _run_compact_with_id_churn(monkeypatch)

    assert "widget 项目" in _production_recall(), "同步 repair 后 production recall 应立即恢复记忆"
    new_ids = _evo_node_ids()
    assert new_ids and not (old_ids & new_ids), (
        "restore 应清掉换 id 前的旧 head 孤儿，evo_nodes 只剩 compact 后的新链头"
    )
