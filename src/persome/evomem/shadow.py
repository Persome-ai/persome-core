"""增量影子写 evo_nodes（SSOT 切换设计稿 §4.2 双写影子期，PR-3）。

设计稿：``docs/superpowers/specs/2026-06-10-evomem-ssot-switch-design.md``。

挂点收口：当前全部写站点（chat memory_extractor、chat tool_handlers、
writer/tools、session_reducer、schema_miner_stage、cross_domain_sweeper、
timeline/aggregator）最终都收敛到 ``store/entries.py`` 的三条写路
（``append_entry`` / ``supersede_entry`` / ``mark_entry_deleted``）。影子 hook
挂在这三条写路的锁尾——任何现有/未来 caller 自动被覆盖（设计稿风险 1
「绕过主写+影子机制的写路径」的最强对冲）。唯一已知绕路站点是
``writer/compact.py``（LLM 整文件重写 + rebuild_index），由
:func:`note_out_of_band_rewrite` 把滞后记成可见 miss。

影子写形态 = backfill 单条版：复用 ``backfill.map_entry_to_node`` 同一映射函数 +
``store.upsert_node`` 同一条 SQL，保证核心不变式「增量影子写后的 evo_nodes 态 ==
重跑全量 backfill 的态」（``tests/test_evomem/test_shadow.py`` 以逐字段全等钉死）。

纪律（§4.2）：

- **此期间 markdown 仍是 SSOT，影子是可弃的。** 影子写失败/跳过只记 warning +
  计数，**绝不**回滚或阻塞主写——:func:`after_write` 吞掉一切异常。
- **冷启动衔接**：影子写假设 ``evomem-backfill`` 已跑过。evo_nodes 缺表/为空，
  或本次写涉及的链端点节点不在 evo_nodes（明显落后），都记 miss 跳过而不半建链
  ——修复动作永远是重跑幂等的 backfill。
- **失败可见性**：累计 miss 每满 ``_ALERT_EVERY`` 次经 PR-1 报警通路发一条
  ``integrity_alert``（check=``shadow_write_lag``，alert-only 不冻结），让
  「影子悄悄落后」可见（§3.4 投影滞后可见性的同款关切）。
- **Q2**：``event-`` 前缀豁免，静默跳过（按设计根本不进 evo_nodes，不算 miss）。
- **Q4**：scope 全取 default，与 backfill 默认一致。

指针解析说明：``#superseded-by`` 边由 ``supersede_entry`` 产生，构造上永远是
文件内边，所以这里只在本文件 parse 范围内解析 back-map——与 backfill 的全库
back-map 对这类边产出一致；指向文件外/未知 id 的边按悬空丢弃（与 backfill 的
dangling 处理同口径，绝不写进指针列）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Sequence

from .. import config as config_mod
from ..logger import get
from ..store import files as files_mod
from . import integrity
from . import store as evo_store

_log = get("persome.evomem")

# Q4：影子写 scope 与 backfill 默认一致。
_USER_ID = "default"
_AGENT_ID = "default"

# 失败可见性：累计 miss 每满 N 次发一条 integrity_alert（alert-only）。
_ALERT_EVERY = 5

_miss_lock = threading.Lock()
_miss_count = 0


def miss_count() -> int:
    """累计影子写 miss（失败 + 跳过）次数，进程内计数。"""
    with _miss_lock:
        return _miss_count


def reset_misses() -> None:
    """清零 miss 计数（测试 seam / 人工重跑 backfill 补齐后的复位按钮）。"""
    global _miss_count
    with _miss_lock:
        _miss_count = 0


def _record_miss(detail: str, *, alert: bool = True) -> None:
    """一次影子写没有落库（异常或滞后跳过）：warning + 计数 + 阈值报警。

    报警走 PR-1 的同一条 ``integrity_alert`` 通路（§4.3 判据 4 已验证过端到端），
    check 名 ``shadow_write_lag``、非 structural——影子是可弃投影，落后可由重跑
    backfill 自愈，永远不冻结写口。

    ``alert=False`` 用于冷启动跳过（evo_nodes 空 = backfill 未跑、影子期尚未开始）：
    这不是「影子悄悄落后」，只 warning + 计数，不占报警通路——否则一座从未
    backfill 的库每 5 次写就发一条假报警，报警就贬值了。
    """
    global _miss_count
    with _miss_lock:
        _miss_count += 1
        n = _miss_count
    _log.warning("shadow write miss (cumulative=%d): %s", n, detail)
    if alert and n % _ALERT_EVERY == 0:
        try:
            integrity.emit_alert(
                "shadow_write_lag",
                f"{n} cumulative shadow-write misses; latest: {detail}"
                " — evo_nodes 已落后，重跑 `persome evomem-backfill` 补齐",
                source="shadow_write",
                structural=False,
            )
        except Exception:  # noqa: BLE001 — 报警失败不能再伤害写路径
            _log.warning("shadow_write_lag alert emission failed", exc_info=True)


def after_write(conn: sqlite3.Connection, *, name: str, entry_ids: Sequence[str]) -> None:
    """三条主写路的锁尾影子 hook。绝不抛出，绝不影响主写。

    主写（markdown + FTS/链/旁挂表，autocommit 已落定）完成后调用；本函数把受
    影响的 entry 增量映射进 evo_nodes。任何异常 → warning + miss 计数，调用方
    （``store/entries.py``）继续原样返回。
    """
    try:
        _shadow_write(conn, name=name, entry_ids=[i for i in entry_ids if i])
    except Exception as exc:  # noqa: BLE001 — 影子失败只记账，主写已成功
        _record_miss(f"{name} {list(entry_ids)}: {exc!r}")


def note_out_of_band_rewrite(names: Sequence[str]) -> None:
    """整文件重写绕过三条写路（目前仅 ``writer/compact.py``）：记成可见 miss。

    compact 由 LLM 重写整个 markdown 文件再 ``rebuild_index``，影子无法增量跟进
    （backfill 也只 upsert 不删除，单文件重投影会与「重跑全量 backfill 的态」
    分叉）。诚实的做法是把滞后记账让它可见——修复 = 重跑幂等 backfill。
    event- 前缀照旧豁免。绝不抛出。
    """
    try:
        if not config_mod.load().evomem.shadow_write_enabled:
            return
        from . import inversion

        if inversion.evomem_active():  # 反转模式：影子方向已反，停用（PR-6b）
            return
        for name in names:
            try:
                prefix = files_mod.validate_prefix(files_mod.memory_path(name).name)
            except ValueError:
                continue
            if prefix == "event":
                continue
            _record_miss(f"{name}: 整文件重写（compact）绕过影子写，evo_nodes 对该文件已滞后")
    except Exception:  # noqa: BLE001
        _log.warning("note_out_of_band_rewrite failed", exc_info=True)


def _evo_ready(conn: sqlite3.Connection) -> bool:
    """冷启动守卫：evo_nodes 表存在且本 scope 至少有一行（== backfill 跑过）。"""
    try:
        row = conn.execute(
            "SELECT 1 FROM evo_nodes WHERE user_id=? AND agent_id=? LIMIT 1",
            (_USER_ID, _AGENT_ID),
        ).fetchone()
    except sqlite3.OperationalError:  # 表不存在 = backfill 从未跑过
        return False
    return row is not None


def _shadow_write(conn: sqlite3.Connection, *, name: str, entry_ids: list[str]) -> None:
    cfg = config_mod.load()
    if not cfg.evomem.shadow_write_enabled or not entry_ids:
        return
    # 写权反转（PR-6b §4.4）下影子写自动停用：evomem 主写模式里 evo_nodes 是
    # 真相、markdown 是投影，「从 markdown 解析回灌 evo_nodes」方向反了——还能
    # 走到这里的 legacy 直写只剩 event-*（本就豁免）与 skills/ 子目录等豁免口，
    # 把它们镜像进真相表反而污染真相。翻回 "markdown" 时本 hook 自动恢复（§6）。
    from . import inversion

    if inversion.evomem_active():
        return
    path = files_mod.memory_path(name)
    prefix = files_mod.validate_prefix(path.name)
    if prefix == "event":  # Q2 豁免：append-only 日志不进 evo_nodes，不算 miss
        return
    if not _evo_ready(conn):
        _record_miss(
            f"{name}: evo_nodes 为空/缺表（backfill 未跑）—"
            " 先跑 `persome evomem-backfill` 建立基线",
            alert=False,
        )
        return

    parsed = files_mod.read_file(path)
    by_id = {e.id: e for e in parsed.entries}
    affected: list[files_mod.ParsedEntry] = []
    for eid in entry_ids:
        e = by_id.get(eid)
        if e is None:
            _record_miss(f"{name}: entry {eid} 写后解析缺失，跳过本批影子写")
            return
        affected.append(e)

    # 文件内 back-map（#superseded-by 是 old→new 单向 tag，反向得 supersedes 边）。
    file_ids = set(by_id)
    preds: dict[str, list[str]] = {}
    for e in parsed.entries:
        if e.superseded_by and e.superseded_by in file_ids:
            preds.setdefault(e.superseded_by, []).append(e.id)

    # 前驱缺失守卫（§4.2 冷启动衔接）：本批节点与批外节点之间的每条链边，批外
    # 端点必须已存在于 evo_nodes **且镜像指针已指回批内节点**（该指针只会由更早
    # 那次 supersede 的影子写写下）。端点缺失 → 会写出悬空指针；镜像缺失 → 会写
    # 出单向指针（自检铁律 2 的两种 violation 形态，都说明影子已明显落后于这条
    # 链）。任一情形整批跳过，不半建链——修复 = 重跑幂等 backfill。
    batch = {e.id for e in affected}
    # (external_id, required_mirror_member, mirror_column)
    required_mirrors: list[tuple[str, str, str]] = []
    for e in affected:
        if e.superseded_by and e.superseded_by in file_ids and e.superseded_by not in batch:
            required_mirrors.append((e.superseded_by, e.id, "supersedes"))
        for p in preds.get(e.id, []):
            if p not in batch:
                required_mirrors.append((p, e.id, "superseded_by"))
    if required_mirrors:
        external = sorted({ext for ext, _, _ in required_mirrors})
        placeholders = ",".join("?" * len(external))
        rows = {
            r["node_id"]: r
            for r in conn.execute(
                f"SELECT node_id, supersedes, superseded_by FROM evo_nodes"
                f" WHERE user_id=? AND agent_id=? AND node_id IN ({placeholders})",
                (_USER_ID, _AGENT_ID, *external),
            )
        }
        stale: list[str] = []
        for ext, member, column in required_mirrors:
            row = rows.get(ext)
            if row is None:
                stale.append(f"{ext} 缺失")
            elif member not in json.loads(row[column] or "[]"):
                stale.append(f"{ext}.{column} 未含 {member}")
        if stale:
            _record_miss(
                f"{name}: 链端点在 evo_nodes 缺失/指针未闭合（影子明显落后）:"
                f" {'; '.join(sorted(set(stale)))} — 跳过本批，不半建链"
            )
            return

    # 旁挂表两件套：与 backfill 同源（entry_metadata / entry_temporal）。
    # is_latest 由 markdown tag 三态判定派生（entry_chain 已退役，PR-7）。
    placeholders = ",".join("?" * len(batch))
    ids = sorted(batch)
    metadata = {
        r["entry_id"]: r
        for r in conn.execute(
            f"SELECT entry_id, confidence, conflicted, occurred_at FROM entry_metadata"
            f" WHERE entry_id IN ({placeholders})",
            ids,
        )
    }
    temporal = {
        r["entry_id"]: r
        for r in conn.execute(
            f"SELECT entry_id, valid_from, valid_until FROM entry_temporal"
            f" WHERE entry_id IN ({placeholders})",
            ids,
        )
    }

    # 延迟导入打破环：backfill 顶层 import store.entries，而 store/entries.py
    # 顶层 import 本模块。
    from . import backfill

    nodes = []
    for e in affected:
        nodes.append(
            backfill.map_entry_to_node(
                e,
                file_name=path.name,
                prefix=prefix,
                supersedes=preds.get(e.id, []),
                superseded_by=(
                    [e.superseded_by] if e.superseded_by and e.superseded_by in file_ids else []
                ),
                meta=metadata.get(e.id),
                temporal=temporal.get(e.id),
                user_id=_USER_ID,
                agent_id=_AGENT_ID,
            )
        )

    # 同一事务落整批（supersede = 新旧两节点），半写不留——失败回滚后由
    # after_write 记 miss，主写不受影响（连接是 autocommit，主写早已落定）。
    conn.execute("BEGIN")
    try:
        for node in nodes:
            evo_store.upsert_node(conn, node, user_id=_USER_ID, agent_id=_AGENT_ID)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    _log.debug("shadow write ok: %s → %d node(s)", name, len(nodes))
