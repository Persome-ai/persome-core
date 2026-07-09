"""recall 折叠降级要可见（INT-02/MCP-01 的 PR-7 形态）。

``_hint_layer`` catches ``sqlite3.OperationalError`` and skips the hint. That is
correct for a residual malformed FTS query, but a CORRUPT ``evo_nodes`` table
(present, non-empty, wrong shape) would also raise — and silently swallowing it
makes recall quietly go empty (a hidden regression). The fix logs a warning so
the degradation is visible; behaviour is unchanged (still skip the hint).

注意区分两个形态：evo_nodes **缺表/为空** = 文档化的冷启动路径（折叠退化到
superseded 列，零 warning，见 test_recall_evo_read）；evo_nodes **存在但坏掉**
= 本文件的可见降级路径。
"""

from __future__ import annotations

import logging

from persome.intent import recall
from persome.store import entries as entries_mod
from persome.store import fts


def _seed(conn) -> None:
    entries_mod.create_file(conn, name="project-x.md", description="x", tags=["t"])
    entries_mod.append_entry(conn, name="project-x.md", content="DeepSeek fact", tags=["t"])


def test_corrupt_evo_nodes_logs_warning_not_silent(ac_root, caplog):
    """evo_nodes 表存在且非空但缺折叠列 → fold 查询 OperationalError：记 warning
    且不抛（降级为跳过该 hint，但可见）。"""
    with fts.cursor() as conn:
        _seed(conn)
        # 伪造一张「就绪但坏掉」的 evo_nodes：_evo_fold_ready 的探针列在、
        # 折叠子查询要的 node_id/is_latest/status 不在。
        conn.execute("CREATE TABLE evo_nodes (user_id TEXT, agent_id TEXT)")
        conn.execute("INSERT INTO evo_nodes VALUES ('default', 'default')")
        with caplog.at_level(logging.WARNING, logger="persome.intent.recall"):
            bundle = recall.assemble_background(
                conn, scope="timeline", hints=["DeepSeek"], fold_superseded=True
            )
    assert any("fold query failed" in r.getMessage() for r in caplog.records), (
        f"expected a degradation warning, got: {[r.getMessage() for r in caplog.records]}"
    )
    # did not raise; degraded visibly（hint 被跳过）
    assert isinstance(bundle, str)


def test_malformed_fts_query_still_skipped_without_crash(ac_root):
    """The original contract: a malformed FTS hint is skipped, not raised."""
    with fts.cursor() as conn:
        _seed(conn)
        # 纯标点 hint：转义后为仅含引号的 FTS5 串 → 空 phrase，最坏 OperationalError
        # 也被吞掉跳过，绝不 crash。
        bundle = recall.assemble_background(conn, scope="timeline", hints=['"'])
    assert isinstance(bundle, str)


def test_unfolded_path_unaffected_no_warning(ac_root, caplog):
    """fold OFF 时不跑任何 evo_nodes 查询：坏表也不产生降级 warning——默认
    （未折叠）路径完全不受影响。"""
    with fts.cursor() as conn:
        _seed(conn)
        conn.execute("CREATE TABLE evo_nodes (user_id TEXT, agent_id TEXT)")
        conn.execute("INSERT INTO evo_nodes VALUES ('default', 'default')")
        with caplog.at_level(logging.WARNING, logger="persome.intent.recall"):
            bundle = recall.assemble_background(
                conn, scope="timeline", hints=["DeepSeek"], fold_superseded=False
            )
    assert "DeepSeek" in bundle
    assert not any("fold query failed" in r.getMessage() for r in caplog.records)
