"""import_from_markdown — 从 markdown 投影逆向重建 evo_nodes（SSOT 切换 §3.4，PR-7 定型）。

Restores a verified evomem snapshot.

原 ``rebuild_index`` 的 from-markdown 全量逻辑「反向化」后的最后防线：解析投影
markdown → 按 PR-6a 投影命名空间 colon-tag（``#layer:`` / ``#status:`` /
``#scope:`` / ``#valid-from:`` / ``#valid-until:``）还原 SSOT 字段 → 按
``#superseded-by``/``#refined-from``/``#abstracted-from`` tag 重建双向指针 →
**整库替换**灌 ``evo_nodes``（单事务 DELETE + INSERT）→ 重放检索投影 →
跑 §3.3 全套自检。核心映射 = ``store/projector.py:rebuild_nodes_from_projection``
（round-trip CI 守护的同一逆向半程）。

**四条有损限制（§3.4，诚实标注——这套设施是对冲，不是 markdown-SSOT 时代
「rebuild 零损失自愈」的等价替代）：**

1. **时间精度有损**：heading 只有分钟粒度，``memory_at``/``gmt_created`` 秒级
   精度丢失；链头 tiebreaker 退化到 ``node_id`` 二级键（仍可复现，但与原序
   可能不同）。
2. **投影滞后窗口有损**：增量投影 best-effort，崩溃前最后若干次写可能未投影
   ——这部分写入**永久丢失**（滞后由 ``markdown_projection_lag`` 计数器可见）。
3. **只还原投影编码了的字段**：layer/status/scope/temporal 靠 colon-tag 还原；
   任何后续新增 SSOT 列若忘了同步投影编码，灾难时即丢——投影 round-trip 测试
   （写 → 投影 → 逆向重建 → 字段全等）在 CI 作该原则的机器守护。
4. **定位是灾难恢复的近似还原，不是日常自愈**。日常自愈的对象只剩两个投影
   （FTS / markdown），它们随时可由 ``rebuild_index`` /
   ``evomem-project-markdown`` 从 evo_nodes 全量重建。真相层损坏优先从快照
   恢复（§3.2 ``backup/evo-*.db``）；本工具是快照也没了之后的最后一招。

纪律：

- **执行前快照**（§3.2 变更前快照纪律）：写库前先验证式 ``VACUUM INTO``——
  即便库已损坏，留住「恢复前最后状态」供事后取证；快照失败立即中止。
- **Q2**：``event-*.md`` 豁免（链真相从不进 evo_nodes），跳过。
- 收尾跑 §3.3 全套自检；violations 落入 report、CLI 退出非零。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..logger import get
from ..store import entries as entries_mod
from ..store import files as files_mod
from ..store import fts
from . import backup, integrity
from .store import NodeStore, upsert_node

_log = get("persome.evomem")


class RestoreError(RuntimeError):
    """Raised when the restore must abort before touching evo_nodes."""


@dataclass
class RestoreReport:
    """One restore run's outcome."""

    dry_run: bool
    files: int = 0
    skipped_event_files: int = 0
    nodes: int = 0
    projection_files: int = 0
    projection_entries: int = 0
    violations: list[integrity.Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


def import_from_markdown(*, dry_run: bool = False) -> RestoreReport:
    """从 markdown 投影整库重建 evo_nodes + 检索投影（§3.4 灾难恢复，有损）。

    ``dry_run`` 只解析与映射并统计，不打快照、不写库。非 dry-run 流程：
    验证式快照 → 单事务 ``DELETE FROM evo_nodes`` + 全量 INSERT（节点 scope 由
    ``#scope:`` tag 还原，缺省 default/default）→ ``rebuild_index`` 重放检索
    投影 → §3.3 全套自检。

    Raises :class:`RestoreError` when the §3.2 pre-change snapshot fails.
    """
    from ..store import projector

    report = RestoreReport(dry_run=dry_run)
    parsed_files: list[tuple[str, list[files_mod.ParsedEntry]]] = []
    for path in files_mod.list_memory_files():
        try:
            prefix = files_mod.validate_prefix(path.name)
        except ValueError as exc:
            _log.warning("restore: skipping %s: %s", path.name, exc)
            continue
        if prefix == "event":  # Q2 豁免：链真相从不进 evo_nodes
            report.skipped_event_files += 1
            continue
        parsed_files.append((path.name, files_mod.read_file(path).entries))
        report.files += 1

    nodes = projector.rebuild_nodes_from_projection(parsed_files)
    report.nodes = len(nodes)
    if dry_run:
        return report

    # §3.2 变更前快照：即便主库已带伤，留住恢复前最后状态供取证；坏快照中止。
    if backup.create_snapshot(structural_only=True) is None:
        raise RestoreError(
            "pre-restore snapshot failed (VACUUM INTO / verification) — aborting,"
            " evo_nodes untouched"
        )
    integrity.ensure_writes_allowed()
    NodeStore()  # ensures table + migration
    # Restore REPLACES only the scopes the projection actually rebuilds. An
    # unscoped `DELETE FROM evo_nodes` would also wipe nodes in any scope the
    # scanned/exempt markdown口径 doesn't cover (e.g. non-default scope) — and
    # those never get re-inserted → the disaster tool itself becomes a data-loss
    # source (#583). Delete per (user_id, agent_id) of the rebuilt nodes only.
    rebuilt_scopes = {(node.user_id, node.agent_id) for node in nodes}
    with fts.cursor() as conn:
        conn.execute("BEGIN")
        try:
            for uid, aid in rebuilt_scopes:
                conn.execute("DELETE FROM evo_nodes WHERE user_id = ? AND agent_id = ?", (uid, aid))
            for node in nodes:
                upsert_node(conn, node, user_id=node.user_id, agent_id=node.agent_id)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        # 检索投影从恢复后的真相重放（authority-dispatched：evomem 主写 →
        # evo_nodes 投影 + event-* markdown 直读；markdown 主写 → markdown 重放）。
        report.projection_files, report.projection_entries = entries_mod.rebuild_index(conn)
        report.violations = integrity.run_checks(conn)

    _log.info(
        "import_from_markdown: %d file(s) parsed (%d event-* skipped) → %d node(s),"
        " projection %d file(s)/%d entr(ies), ok=%s",
        report.files,
        report.skipped_event_files,
        report.nodes,
        report.projection_files,
        report.projection_entries,
        report.ok,
    )
    return report
