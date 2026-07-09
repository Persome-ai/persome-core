"""issue #526 回归：切主读后 compact 致 evo_nodes 丢记忆 → 自动 repair 自修。

链条（复现）：
- compact（markdown 主写模式）LLM 整文件重写 + ``rebuild_index`` 绕过三条写路，
  给条目换新 id；
- 旧行为只 ``note_out_of_band_rewrite`` 记 alert-only miss，daemon 无自动修复；
- ``recall_fold_superseded`` 主读默认走 ``_EVO_FOLD_SQL``，要求非 event 条目在
  evo_nodes 且 ``is_latest=1 AND status='active'``——compact 后新 id 不在 evo_nodes，
  从折叠 recall 的行为/事实/关键词层直接消失。

修复：``run_pending`` accept 后自动 enqueue 一条幂等 ``evomem-compact-repair`` agent
run，dispatcher 消费后用 ``restore.import_from_markdown`` 从 markdown SSOT 整库重建
evo_nodes（清掉换 id 留下的孤儿 head），折叠 recall 恢复——「报警等人工跑 CLI」升级为
「daemon 自修」。
"""

from __future__ import annotations

import asyncio
import re

from persome import config as config_mod
from persome.config import load as load_config
from persome.evomem import backfill
from persome.intent import recall
from persome.runs import dispatcher
from persome.store import entries as entries_mod
from persome.store import files as files_mod
from persome.store import fts
from persome.writer import compact as compact_mod
from persome.writer import llm as llm_mod

_KIND = "evomem-compact-repair"
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


def _fold_recall() -> str:
    with fts.cursor() as conn:
        return recall.assemble_background(
            conn, scope="", hints=[_HINT], per_hint=10, fold_superseded=True
        )


def _repair_runs() -> list[dict[str, str]]:
    """直接查 agent_runs 表里所有自修 run（避开窗口过滤的 tz 细节）。"""
    with fts.cursor() as conn:
        rows = conn.execute(
            "SELECT id, status, trigger FROM agent_runs WHERE kind = ? ORDER BY enqueued_at ASC",
            (_KIND,),
        ).fetchall()
    return [{"id": r["id"], "status": r["status"], "trigger": r["trigger"]} for r in rows]


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


def test_compact_evicts_memory_then_enqueues_repair(ac_root, monkeypatch) -> None:
    """复现 + enqueue 验证：compact 换 id 后折叠 recall 丢记忆，同时落了一条
    queued 的 evomem-compact-repair 自修 run。"""
    monkeypatch.setattr("persome.events.publish", lambda *a, **k: None)
    _seed_and_backfill()
    assert "widget 项目" in _fold_recall(), "backfill 后折叠 recall 应能看到记忆"

    _run_compact_with_id_churn(monkeypatch)

    # 复现：记忆从折叠 recall 消失（evo_nodes 仍是旧 id）。
    assert "widget 项目" not in _fold_recall(), (
        "BUG #526 复现：compact 后新 id 不在 evo_nodes，折叠 recall 丢失记忆"
    )

    # 修复：compact accept 自动 enqueue 了一条自修 run。
    runs = _repair_runs()
    assert runs, "compact accept 应 enqueue 一条 evomem-compact-repair 自修 run"
    assert runs[0]["trigger"] == "compact"
    assert runs[0]["status"] == "queued"


def test_dispatcher_repair_run_heals_recall_and_clears_orphans(ac_root, monkeypatch) -> None:
    """端到端自愈：compact 丢记忆后，dispatcher 消费 repair run，从 markdown 整库
    重建 evo_nodes——折叠 recall 恢复，且换 id 留下的旧 head 孤儿被清除。"""
    monkeypatch.setattr("persome.events.publish", lambda *a, **k: None)
    _seed_and_backfill()
    old_ids = _evo_node_ids()
    assert old_ids, "backfill 后 evo_nodes 应有链头"

    _run_compact_with_id_churn(monkeypatch)
    assert "widget 项目" not in _fold_recall(), "compact 后应先丢记忆（待 repair 自修）"

    # repair executor（restore-from-markdown）无 LLM 调用，compact 残留的 llm mock
    # 不影响它；不 undo monkeypatch（那会连 ac_root 设的 PERSOME_ROOT 一起撤）。
    cfg = load_config()

    async def drive() -> None:
        await dispatcher.drain_once(cfg)
        for _ in range(80):
            runs = _repair_runs()
            run = runs[0] if runs else None
            if run is not None and run["status"] in ("committed", "skipped", "failed"):
                break
            await asyncio.sleep(0.05)

    asyncio.run(drive())

    run = _repair_runs()[0]
    assert run["status"] == "committed", f"repair run 应成功提交，实际 {run['status']}"

    # 自愈：折叠 recall 重新看到记忆（evo_nodes 已是 compact 后的新 id 链头）。
    assert "widget 项目" in _fold_recall(), "#526 自修：repair run 消费后折叠 recall 应恢复记忆"
    # 孤儿清除：restore 整库替换，旧 head 不再残留（upsert-only 的 backfill 会留孤儿）。
    new_ids = _evo_node_ids()
    assert new_ids and not (old_ids & new_ids), (
        "restore 应清掉换 id 前的旧 head 孤儿，evo_nodes 只剩 compact 后的新链头"
    )


def test_repair_kind_registered_and_capped(ac_root) -> None:
    """evomem-compact-repair kind 已注册且并发上限为 1（幂等串行）。"""
    from persome.runs.registry import KIND_REGISTRY

    assert _KIND in KIND_REGISTRY
    assert dispatcher.CONCURRENCY.get(_KIND) == 1
