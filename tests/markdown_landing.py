"""测试侧的 op→markdown 落地 helper（``writer/reconcile_apply.py`` 删除后的遗产）。

适配层本体已在 PR-6b 删除（翻译职责由 evomem engine 原生承担，反转写口见
``evomem/inversion.py``）；但它定义过的 **legacy markdown 落地形态**——UPDATE =
``supersede_entry(refined_from=old)`` 双标签法、ABSTRACT = append 带
``abstracted-from`` 多值 tag + 逐源 ``mark_entry_deleted``（链语义②）——仍是
``store/entries.py`` 写口的回归基线，由既有测试（test_evo02_refined_from /
test_write02_abstract）继续钉死。本模块只为这些
测试保留两条最小落地序列，**不是生产代码**。
"""

from __future__ import annotations

import sqlite3

from persome.evomem.models import ReconcileOp
from persome.store import entries as entries_mod


def apply_update(
    conn: sqlite3.Connection, op: ReconcileOp, *, file_name: str, tags: list[str] | None = None
) -> str:
    """UPDATE 的 legacy markdown 落地：退役旧版本 + 新头带 #refined-from（EVO-02）。"""
    assert op.target_id is not None
    return entries_mod.supersede_entry(
        conn,
        name=file_name,
        old_entry_id=op.target_id,
        new_content=op.content,
        reason=op.reason,
        tags=tags or None,
        refined_from=op.target_id,
    )


def apply_abstract(
    conn: sqlite3.Connection, op: ReconcileOp, *, file_name: str, tags: list[str] | None = None
) -> str:
    """ABSTRACT 的 legacy markdown 落地：合成条目带多值出处 tag + 逐源 strike（链语义②）。"""
    assert len(op.source_ids) >= 2
    entry_tags = list(tags or []) + ["abstracted-from:" + ",".join(op.source_ids)]
    new_id = entries_mod.append_entry(conn, name=file_name, content=op.content, tags=entry_tags)
    for source_id in op.source_ids:
        entries_mod.mark_entry_deleted(conn, name=file_name, entry_id=source_id)
    return new_id
