"""§1.5-2 图侧孤儿收敛 —— 长不出实质边的点，到期按噪声遗忘。

图的宪法（§1.5）：只有 USER 所在连通块算记忆；连不上的点入 shadow 孤儿区 + TTL，
到期遗忘。**单连通靠遗忘收敛，不靠万能边。** 这是「过度生产（delta apply 读全场多铸）
+ 遗忘收敛」闭环的收敛腿——delta 敢多铸一次性工作项，正因为长不出实质边的会在这里被忘。

判据（保守，宁缺毋滥地忘）：一个 entity 点（person/org/project/artifact）被遗忘，当且仅当
1. **孤儿**：其 identity 无任何 open（valid_to IS NULL）的 6 谓词边（active/shadow 皆算——
   连 shadow 边都没有 = 真孤立）；有边 = 参与图 = 留。
2. **到期**：`gmt_created` 龄 > `ttl_days`（默认 30）。
3. 无边即无 recall（读走边，§3.3），故「读即免疫」由孤儿判据天然覆盖。

遗忘 = `mark_entry_deleted`（markdown strike，**收据留在盘上**、FTS superseded、可回放）——
非物理删（§1.5-4 append-only）。event:* 终态点、self 不在收割范围。config-gated、fail-open。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ..config import Config
from ..evomem.engine import EvoMemory
from ..logger import get

logger = get("persome.writer.orphan_reaper")

# 收割范围：实体点前缀（event-* 终态、self、schema/thread/skill/… 不进）
_ENTITY_PREFIXES = ("person-", "org-", "project-", "tool-")


@dataclass
class ReapResult:
    candidates: int = 0
    reaped: int = 0
    reaped_files: list[str] = field(default_factory=list)
    skipped_reason: str = ""


def find_orphans(
    conn: sqlite3.Connection,
    *,
    ttl_days: int,
    now: datetime | None = None,
    engaged_keep: int = 2,
) -> list[tuple[str, str, str]]:
    """返回可**驱逐**的实体点 (node_id, file_name, canonical)——**注意力驱逐**（H2O）。

    两层模型下每个语境实体都有一条 ``engaged_with`` 地板边（连通永不空），所以不再看
    「有没有边」，看**注意力强弱**：一个点被驱逐当且仅当（∧龄>ttl）
    1. **无②层语义结构边**（除 engaged_with 外任何谓词都算「有结构」→ 留），且
    2. **地板边弱**：engaged_with 的 max observations < ``engaged_keep``（即只碰过一两次、
       再没回来 = 低注意力）。反复参与（obs≥keep）或长出结构边 = 高注意力 → 留。
    """
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(days=ttl_days)).isoformat()
    like = " OR ".join(f"file_name LIKE '{p}%'" for p in _ENTITY_PREFIXES)
    conn.row_factory = None
    # 只针对**实体点**行（tags='entity'，content=规范名）——一个实体文件里除了这一行还有
    # 一堆事实条目（assertions，content=事实句、tags='fact …'）；不加此闸会把每个事实行
    # 当成 content=规范名的实体点，边匹配全落空 → 误把事实条目当孤儿收割。delta 铸点
    # (`_apply_entities` `add_direct(canonical, tags="entity")`) 是 content=canonical 的唯一来源。
    rows = conn.execute(
        f"SELECT node_id, file_name, content FROM evo_nodes "
        f"WHERE is_latest = 1 AND status = 'active' AND tags = 'entity' AND ({like}) "
        f"AND COALESCE(gmt_created, memory_at, '') < ? "
        f"AND COALESCE(gmt_created, memory_at, '') != ''",
        (cutoff,),
    ).fetchall()
    evictable: list[tuple[str, str, str]] = []
    for node_id, file_name, content in rows:
        canonical = (content or "").strip()
        if not canonical:
            continue
        # ②层结构边（非 engaged_with）存在 → 高注意力，留
        structural = conn.execute(
            "SELECT 1 FROM relation_edges WHERE valid_to IS NULL "
            "AND status IN ('active','shadow') AND predicate != 'engaged_with' "
            "AND (src_identity = ? OR dst_identity = ?) LIMIT 1",
            (canonical, canonical),
        ).fetchone()
        if structural:
            continue
        # 只剩地板边：看注意力强度（max observations）
        row = conn.execute(
            "SELECT COALESCE(MAX(observations),0) FROM relation_edges WHERE valid_to IS NULL "
            "AND status IN ('active','shadow') AND (src_identity = ? OR dst_identity = ?)",
            (canonical, canonical),
        ).fetchone()
        if int(row[0] if row else 0) < engaged_keep:
            evictable.append((node_id, file_name, canonical))
    return evictable


def run_orphan_reap(
    cfg: Config,
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    memory: EvoMemory | None = None,
) -> ReapResult:
    """一次夜检收割。Self-gated on ``[orphan_reaper] enabled``（默认 OFF）。"""
    r = ReapResult()
    orc = getattr(cfg, "orphan_reaper", None)
    if not getattr(orc, "enabled", False):
        r.skipped_reason = "disabled"
        return r
    ttl_days = int(getattr(orc, "ttl_days", 30))
    engaged_keep = int(getattr(orc, "engaged_keep", 2))
    try:
        orphans = find_orphans(conn, ttl_days=ttl_days, now=now, engaged_keep=engaged_keep)
    except Exception:  # noqa: BLE001 — 缺表/异常 fail-open：本轮不收割
        logger.warning("orphan reap: scan failed", exc_info=True)
        r.skipped_reason = "scan_failed"
        return r
    r.candidates = len(orphans)
    mem = memory or EvoMemory()
    stamp = (now or datetime.now(UTC)).isoformat()
    max_per_night = int(getattr(orc, "max_per_night", 200))
    for node_id, file_name, _canonical in orphans[:max_per_night]:
        try:
            # 软遗忘：引擎 retire（status=shadow, is_latest=0，收据行留）——与 delta_apply
            # 铸点同走 EvoMemory，图/投影一致；append-only（不物理删）。
            mem.commit_retire(node_id, valid_until=stamp)
            r.reaped += 1
            r.reaped_files.append(file_name)
        except Exception:  # noqa: BLE001 — 单点失败不拖累其余
            logger.warning("orphan reap: forget %s failed", file_name, exc_info=True)
    if r.reaped:
        logger.info(
            "orphan reap: %d/%d orphan point(s) forgotten (TTL %dd)",
            r.reaped,
            r.candidates,
            ttl_days,
        )
    return r
