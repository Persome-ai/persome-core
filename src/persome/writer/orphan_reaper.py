"Evidence-based retirement of low-attention orphan entities."

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ..config import Config
from ..evomem.engine import EvoMemory
from ..logger import get

logger = get("persome.writer.orphan_reaper")


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
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(days=ttl_days)).isoformat()
    like = " OR ".join(f"file_name LIKE '{p}%'" for p in _ENTITY_PREFIXES)
    conn.row_factory = None

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

        structural = conn.execute(
            "SELECT 1 FROM relation_edges WHERE valid_to IS NULL "
            "AND status IN ('active','shadow') AND predicate != 'engaged_with' "
            "AND (src_identity = ? OR dst_identity = ?) LIMIT 1",
            (canonical, canonical),
        ).fetchone()
        if structural:
            continue

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
    r = ReapResult()
    orc = getattr(cfg, "orphan_reaper", None)
    if not getattr(orc, "enabled", False):
        r.skipped_reason = "disabled"
        return r
    ttl_days = int(getattr(orc, "ttl_days", 30))
    engaged_keep = int(getattr(orc, "engaged_keep", 2))
    try:
        orphans = find_orphans(conn, ttl_days=ttl_days, now=now, engaged_keep=engaged_keep)
    except Exception:  # noqa: BLE001
        logger.warning("orphan reap: scan failed", exc_info=True)
        r.skipped_reason = "scan_failed"
        return r
    r.candidates = len(orphans)
    mem = memory or EvoMemory()
    stamp = (now or datetime.now(UTC)).isoformat()
    max_per_night = int(getattr(orc, "max_per_night", 200))
    for node_id, file_name, _canonical in orphans[:max_per_night]:
        try:
            mem.commit_retire(node_id, valid_until=stamp)
            r.reaped += 1
            r.reaped_files.append(file_name)
        except Exception:  # noqa: BLE001
            logger.warning("orphan reap: forget %s failed", file_name, exc_info=True)
    if r.reaped:
        logger.info(
            "orphan reap: %d/%d orphan point(s) forgotten (TTL %dd)",
            r.reaped,
            r.candidates,
            ttl_days,
        )
    return r
