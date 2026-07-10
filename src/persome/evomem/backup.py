"""每日快照 + 保留策略 + 坏快照报警（SSOT 切换设计稿 §3.2，PR-1 生存性设施）。

Creates and verifies bounded SQLite snapshots.

诚实账本（§3.1，原话不许粉饰）：现行架构的王牌是「DB 是可弃投影，rebuild_index 从
markdown 重放零损失自愈」。evo_nodes 升格 SSOT 后这张王牌没了——DB 损坏 = 数据丢失。
本模块（连同 WAL checkpoint 纪律与 ``evomem/integrity.py`` 自检）是**对冲，不是等价
替代**：快照只能把丢失窗口压到一天以内，不能把代价变回零。

机制：

- 每日快照：``VACUUM INTO backup/evo-YYYYMMDD.db``（SQLite 原生在线一致性快照，
  不锁写）。挂在 daemon 既有的 23:55 daily-safety-net tick 末尾、**紧跟**已有的
  ``PRAGMA wal_checkpoint(TRUNCATE)`` 之后——checkpoint 先行保证快照取到的主库是
  新的（§3.2 顺序）。
- 快照验证：先落到 ``.tmp``，对快照文件跑一遍 §3.3 自检；通过才原子 promote 到
  最终名（同日重跑 = 原子替换）。**坏快照立即报警（``integrity_alert`` SSE +
  logger.error）并丢弃 tmp，绝不静默覆盖已有的好快照。**
- 保留策略：近 ``snapshot_keep_daily`` 日逐日 + 近 ``snapshot_keep_weekly`` 周逐周
  （每周留周一的），过期自动清理。未来日期（时钟回拨）防御性保留。

变更前快照（设计稿 §3.2，issue #489 已闭环）：任何 schema migration / backfill 在改
库前**强制**一次验证式 ``VACUUM INTO`` 变更前快照，由框架层兜底而非靠人记得；快照
失败一律 fail-fast 中止，绝不在无救生艇状态下做破坏性变更：

- evo_nodes schema 迁移（``store.py:_migrate`` 的 ``ALTER TABLE``）：确有缺列要补时
  强制 ``create_snapshot(structural_only=True)`` 后才改 schema，失败 →
  ``store.MigrationSnapshotError``，schema 原样不动。
- 一次性数据搬运（``backfill.run_backfill`` / ``restore.import_from_markdown``）：
  写 evo_nodes 前同样强制该快照，失败即 ``raise`` 中止。

``structural_only`` 让这条变更前快照只把结构性违例（§3.3 1–5 类）当失败、对 alert-only
的投影对账类（check 6）放行——见 ``create_snapshot`` docstring。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

from .. import paths
from ..config import Config
from ..logger import get
from . import integrity

_log = get("persome.evomem")

_SNAPSHOT_RE = re.compile(r"^evo-(\d{8})\.db$")


def _local_today(now: datetime | None = None) -> date:
    return (now or datetime.now().astimezone()).date()


def snapshot_path(day: date) -> Path:
    return paths.backup_dir() / f"evo-{day.strftime('%Y%m%d')}.db"


def create_snapshot(
    *,
    db_path: Path | None = None,
    now: datetime | None = None,
    structural_only: bool = False,
) -> Path | None:
    """Take today's verified snapshot. Returns its path, or ``None`` on failure.

    Sequence: ``VACUUM INTO`` a ``.tmp`` sibling → run the §3.3 check suite
    against the tmp file → atomically promote (``rename``) over today's name.
    A snapshot that fails verification is alerted (``integrity_alert``) and
    discarded — an existing good snapshot is never overwritten by a bad one.
    Never raises: any failure alerts and returns ``None`` so the daily tick
    survives.

    ``structural_only``（PR-2 变更前快照用）：只把**结构性**违例（§3.3 1–5 类）当
    verification 失败；alert-only 的投影对账类（check 6）照常报警但不否决快照——
    投影坏 = 可自愈侧，而 backfill 本身就是修齐投影对账的动作，若被它否决则
    幂等重跑永远无法自愈。每日 tick 保持默认 ``False``（任何违例都否决）。"""
    from ..store import fts  # local import keeps module import light

    dest = snapshot_path(_local_today(now))
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        paths.backup_dir().mkdir(parents=True, exist_ok=True)
        tmp.unlink(missing_ok=True)
        with fts.cursor(db_path) as conn:
            conn.execute("VACUUM INTO ?", (str(tmp),))
    except (sqlite3.Error, OSError) as e:
        integrity.emit_alert(
            "snapshot_failed",
            f"VACUUM INTO {tmp.name} failed: {e}",
            source="snapshot",
        )
        tmp.unlink(missing_ok=True)
        return None

    violations = integrity.verify_snapshot(tmp)
    for v in violations:
        integrity.emit_alert(
            "snapshot_verification",
            f"{tmp.name}: {v.check}: {v.detail}",
            source="snapshot",
            structural=v.structural,
        )
    blocking = [v for v in violations if v.structural] if structural_only else violations
    if blocking:
        tmp.unlink(missing_ok=True)
        _log.error(
            "snapshot %s FAILED verification (%d violation(s), %d blocking) — discarded, "
            "existing snapshot (if any) preserved",
            dest.name,
            len(violations),
            len(blocking),
        )
        return None

    tmp.replace(dest)  # atomic same-day refresh
    # The snapshot is already promoted and successful at this point. The size is
    # for an info log only, so a concurrent cleanup of ``dest`` (e.g. a later
    # ``apply_retention`` in the same daily tick) racing the ``stat()`` must NOT
    # turn a successful snapshot into a raised exception — that would both
    # violate the documented never-raises contract and make the caller
    # (``run_daily_backup``) mis-report a good snapshot as a failure (#488).
    try:
        size = dest.stat().st_size
    except OSError:
        size = -1
    _log.info("snapshot written: %s (%d bytes)", dest.name, size)
    return dest


def apply_retention(
    *,
    keep_daily: int = 7,
    keep_weekly: int = 4,
    now: datetime | None = None,
) -> list[Path]:
    """Delete snapshots outside the retention policy; return what was removed.

    Keep a snapshot iff its date is within the last ``keep_daily`` days, OR it
    is a Monday within the last ``keep_weekly`` weeks. Unparseable names and
    future-dated files (clock skew) are kept defensively."""
    backup = paths.backup_dir()
    if not backup.is_dir():
        return []
    today = _local_today(now)
    removed: list[Path] = []
    for p in sorted(backup.iterdir()):
        m = _SNAPSHOT_RE.match(p.name)
        if not m:
            continue
        try:
            d = datetime.strptime(m.group(1), "%Y%m%d").date()
        except ValueError:
            continue
        age = (today - d).days
        if age < 0:  # future-dated — keep, defensive
            continue
        keep = age < keep_daily or (d.weekday() == 0 and age < keep_weekly * 7)
        if not keep:
            try:
                p.unlink()
                removed.append(p)
            except OSError as e:
                _log.warning("retention: failed to remove %s: %s", p.name, e)
    if removed:
        _log.info("retention: removed %d expired snapshot(s)", len(removed))
    return removed


def run_daily_backup(
    cfg: Config, *, db_path: Path | None = None, now: datetime | None = None
) -> Path | None:
    """The daily-tick entry point: snapshot (verified) + retention. Never raises."""
    dest = create_snapshot(db_path=db_path, now=now)
    try:
        apply_retention(
            keep_daily=cfg.evomem.snapshot_keep_daily,
            keep_weekly=cfg.evomem.snapshot_keep_weekly,
            now=now,
        )
    except Exception as e:  # noqa: BLE001 — retention must not kill the tick
        _log.warning("retention pass failed: %s", e)
    return dest
