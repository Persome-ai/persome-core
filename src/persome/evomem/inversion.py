"""写权反转（SSOT 切换设计稿 §4.4「切主写」，PR-6b）。

Inverts evomem state back into the selected public write authority.

``[evomem] write_authority = "evomem"`` 时，本模块接管 ``store/entries.py`` 的
全部写口动词（create/append/supersede/delete/set_file_status）。对每个写口
（§1.1「一个真相，两个投影，一个写口」）：

1. **真相写**：按 ``backfill.map_entry_to_node`` 的共享映射备好 ``MemoryNode``
   （写口先把本次写渲染成与 markdown 主写逐字相同的 heading+body 块、再用同一
   解析器解析、再过同一映射——节点形态与 backfill/影子写不可能漂移），经
   ``engine.commit_node``/``commit_supersede``/``commit_retire``（写口栅栏 +
   单事务原子）落 evo_nodes。
2. **FTS 检索投影**（§1.4/Q7，同步维护）：复用 ``store/entries.py`` 的
   ``derived_*_rows`` 同一组 helper 维护 entries/entry_temporal/entry_metadata
   ——派生行的取值与顺序只存在一份，``superseded`` 列的派生规则
   ``0 iff (is_latest=1 AND status='active')`` 由写形态 by construction 满足
   （新节点必为活跃链头 → 0；被退役节点必 shadow → 1）。entry_chain 与双读
   对账机器已在 PR-7 退役。
3. **markdown 人读投影**（§1.5，best-effort）：按 file_name 重投影整个文件到
   live ``memory/``（frontmatter 带 ``projected:`` 注记，Q1 (b)），并记录
   ``projection_state`` 内容 hash（手改检测的对照基准）。**失败只记 warning +
   计数，每满 ``_ALERT_EVERY`` 次发 ``integrity_alert``
   （check=``markdown_projection_lag``，alert-only），绝不回滚真相写**——投影
   滞后是设计本意（与「FTS 落后于 markdown」同构，方向反了）。

挂点收口：与 PR-3 影子写同一选择——**挂在 choke point**（九个写站点全部收敛到
的 ``store/entries.py`` 写口动词），而非逐站点改 import。任何现有/未来 caller
（含 ``write_preset_files``、timeline skill echo、测试）自动被覆盖，是设计稿
风险 1「绕过主写+投影机制的写路径静默分裂」的最强对冲。

豁免口（dispatch 判定 ``routes_to_engine``）：

- **event-***（Q2 裁定）：行为日志量大、append-only、永不入链，留旧 markdown
  直写口；engine 落库入口另有硬拒兜底。
- **skills/ 子目录文件**：``evo_nodes.file_name`` 无子目录信息（与影子写的
  ``path.name`` 口径一致），投影无法路由回子目录，留旧直写口。
- 非法前缀：返回 False，让 legacy 路径抛它原有的 ``ValueError``（错误面不变）。

``write_authority = "markdown"``（默认）时本模块完全旁路：dispatch 是纯 flag
检查，三条主写路 + 影子写行为与现状字节等价（P0）；回滚（§6）= 翻回 flag，
影子双写自动恢复工作。

op 决策与 reconcile 调和的关系（§1.3 的实现取舍）：到达写口动词时，各站点的
写决策已经做完（chat 抽取 / classifier / schema miner 各自的 LLM 或确定性
逻辑）——本模块把它们一一映射为确定性的真相写，不调 reconciler 重新决策，
这是「同输入 → 新旧路径 markdown 投影 byte-identical」迁移纪律的前提；
reconcile 调和（``engine.add``）作为语义升级与写权反转解耦，留待后续按站点
显式启用。
"""

from __future__ import annotations

import dataclasses
import hashlib
import sqlite3
import threading
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import config as config_mod
from ..logger import get
from ..store import files as files_mod
from ..store import fts
from . import integrity

if TYPE_CHECKING:
    from .engine import EvoMemory
    from .models import MemoryNode

_log = get("persome.evomem")

# Q4：反转写口 scope 全取 default，与 backfill / 影子写一致。
_USER_ID = "default"
_AGENT_ID = "default"

# 投影失败可见性（§1.5/§3.4）：累计 miss 每满 N 次发一条 integrity_alert。
_ALERT_EVERY = 5

_miss_lock = threading.Lock()
_miss_count = 0


# ── 写权判定（choke-point dispatch 的谓词）──────────────────────────────────


def authority() -> str:
    """归一化的写权值；未知值告警并回退 ``markdown``（fail-safe 到现状）。"""
    raw = (config_mod.load().evomem.write_authority or "markdown").strip().lower()
    if raw not in ("markdown", "evomem"):
        _log.warning("unknown [evomem] write_authority %r — falling back to 'markdown'", raw)
        return "markdown"
    return raw


def evomem_active() -> bool:
    return authority() == "evomem"


def routes_to_engine(name: str) -> bool:
    """choke-point dispatch 判定：evomem 主写 **且** 目标文件属 evo_nodes 范围。

    见模块 docstring「豁免口」。必须不抛错：非法名交还 legacy 路径抛原有错误。
    """
    try:
        if "/" in name:
            return False  # skills/ 子目录：投影无法路由回子目录，留旧直写口
        if files_mod.validate_prefix(name) == "event":
            return False  # Q2：event-* append-only 日志永不进 evo_nodes
    except ValueError:
        return False
    return evomem_active()


# ── 投影失败计数（镜像 shadow.py 的 miss 模式）──────────────────────────────


def miss_count() -> int:
    """累计 markdown 投影 miss 次数，进程内计数。"""
    with _miss_lock:
        return _miss_count


def reset_misses() -> None:
    """清零 miss 计数（测试 seam / 人工全量重投影补齐后的复位按钮）。"""
    global _miss_count
    with _miss_lock:
        _miss_count = 0


def _record_miss(detail: str) -> None:
    """一次 markdown 投影没有落盘：warning + 计数 + 阈值报警，真相写不受影响。"""
    global _miss_count
    with _miss_lock:
        _miss_count += 1
        n = _miss_count
    _log.warning("markdown projection miss (cumulative=%d): %s", n, detail)
    if n % _ALERT_EVERY == 0:
        try:
            integrity.emit_alert(
                "markdown_projection_lag",
                f"{n} cumulative markdown-projection misses; latest: {detail}"
                " — 人读投影已落后，跑 `persome evomem-project-markdown --live` 补齐",
                source="write_inversion",
                structural=False,
            )
        except Exception:  # noqa: BLE001 — 报警失败不能再伤害写路径
            _log.warning("markdown_projection_lag alert emission failed", exc_info=True)


# ── 内部构件 ────────────────────────────────────────────────────────────────


def _engine() -> EvoMemory:
    """每次写构造（NodeStore 不持连接，建表 DDL 幂等）——避免跨 PERSOME_ROOT
    （测试逐用例换 root）缓存到失效的库。"""
    from .engine import EvoMemory

    return EvoMemory(user_id=_USER_ID, agent_id=_AGENT_ID)


def _file_row(conn: sqlite3.Connection, path_name: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM files WHERE path=?", (path_name,)
    ).fetchone()
    return row


def _load_file_nodes(conn: sqlite3.Connection, path_name: str) -> list[MemoryNode]:
    from . import store as evo_store

    rows = conn.execute(
        "SELECT * FROM evo_nodes WHERE user_id=? AND agent_id=? AND file_name=? ORDER BY node_id",
        (_USER_ID, _AGENT_ID, path_name),
    ).fetchall()
    return [evo_store._row_to_node(r) for r in rows]


def _node_from_write(
    *,
    entry_id: str,
    ts: str,
    tags: list[str],
    body: str,
    path_name: str,
    prefix: str,
    supersedes: list[str],
    confidence: str | None,
    conflicted: bool,
    occurred_at: str | None,
    valid_from: str,
) -> MemoryNode:
    """把一次写口调用变成 MemoryNode——走「渲染 → 解析 → 共享映射」三段。

    先用 ``render_heading`` 渲染出与 markdown 主写**逐字相同**的 entry 块，再用
    同一解析器（``_parse_entries``）解析，再过 ``backfill.map_entry_to_node``
    同一映射。节点形态与「legacy 直写该块后跑 backfill/影子写」的产物 by
    construction 一致——这是逐站「投影 byte-identical」断言的根基。
    """
    from . import backfill

    heading = files_mod.render_heading(timestamp=ts, entry_id=entry_id, tags=tags)
    block = f"{heading}\n{body}\n"
    # Take the first entry (our rendered heading) rather than tuple-unpacking a
    # single element: if ``body`` contains a line that matches ENTRY_HEADING_RE
    # (a user pasting another memory entry verbatim), _parse_entries returns ≥2
    # and the unpack would ValueError, hard-failing this evomem-authority write —
    # while the markdown path treats the same content as ordinary body (#577).
    entries = files_mod._parse_entries(block)
    entry = entries[0]
    return backfill.map_entry_to_node(
        entry,
        file_name=path_name,
        prefix=prefix,
        supersedes=supersedes,
        superseded_by=[],
        meta={
            "confidence": fts._norm_confidence(confidence),
            "conflicted": 1 if conflicted else 0,
            "occurred_at": occurred_at,
        },
        temporal={"valid_from": valid_from, "valid_until": None},
        user_id=_USER_ID,
        agent_id=_AGENT_ID,
    )


def _row_mapping(row: sqlite3.Row | fts.FileRow | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(row, fts.FileRow):
        return dataclasses.asdict(row)
    if isinstance(row, sqlite3.Row):
        return dict(row)
    return row


def record_projection_state(conn: sqlite3.Connection, file_name: str, text: str) -> None:
    """记录一次成功投影的内容 hash（手改检测的对照基准，PR-6b Q1 (b)）。"""
    conn.execute(
        "INSERT INTO projection_state(file_name, content_hash, projected_at)"
        " VALUES (?, ?, ?)"
        " ON CONFLICT(file_name) DO UPDATE SET"
        " content_hash=excluded.content_hash, projected_at=excluded.projected_at",
        (file_name, content_hash(text), datetime.now().astimezone().isoformat(timespec="seconds")),
    )


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _project(
    conn: sqlite3.Connection,
    *,
    path: Path,
    nodes: list,
    file_row: sqlite3.Row | fts.FileRow | Mapping[str, Any],
) -> None:
    """best-effort markdown 投影：失败 warning + 计数 + 阈值报警，绝不抛出。"""
    from ..store import projector

    try:
        text = projector.render_projection(
            path.name, nodes, file_row=_row_mapping(file_row), marker=True
        )
        files_mod.atomic_write_text(path, text)
        record_projection_state(conn, path.name, text)
    except Exception as exc:  # noqa: BLE001 — 投影是可弃派生，真相写已落定
        _record_miss(f"{path.name}: {exc!r}")


def _finish_file_write(
    conn: sqlite3.Connection,
    *,
    path: Path,
    prefix: str,
    soft_limit_tokens: int | None = None,
) -> None:
    """一次写后的文件级收尾：files 行（entry_count/updated/needs_compact）+ 投影。

    ``entry_count`` 以真相侧节点数为准（§1.5——frontmatter 的 files 表元数据由
    投影器维护，files 行是它的数据源）；soft-limit 估算用投影正文
    （``render_content``），与 markdown 主写的 ``post.content`` 同口径、同字节。
    """
    from ..store import projector

    name = path.name
    nodes = _load_file_nodes(conn, name)
    row = _file_row(conn, name)
    needs_compact = bool(row["needs_compact"]) if row is not None else False
    if soft_limit_tokens is not None and not needs_compact:
        content = projector.render_content(name, nodes)
        est_tokens = len(content) // 4
        if est_tokens > soft_limit_tokens:
            needs_compact = True
            _log.info(
                "flagged %s for compact (est %d tokens > %d)", name, est_tokens, soft_limit_tokens
            )
    file_row = fts.FileRow(
        path=name,
        prefix=prefix,
        description=(row["description"] or "") if row is not None else "",
        tags=(row["tags"] or "") if row is not None else "",
        status=(row["status"] or "active") if row is not None else "active",
        entry_count=len(nodes),
        created=(row["created"] or "") if row is not None else "",
        updated=files_mod.today(),
        needs_compact=1 if needs_compact else 0,
    )
    fts.upsert_file(conn, file_row)
    _project(conn, path=path, nodes=nodes, file_row=file_row)


# ── 写口动词（evomem 主写形态；签名镜像 store/entries.py）───────────────────


def create_file(
    conn: sqlite3.Connection,
    *,
    name: str,
    description: str,
    tags: list[str],
    status: str = "active",
) -> Path:
    """evomem 主写的 create：files 行是文件级元数据的真相（无节点可写），投影出
    与 legacy ``write_file`` 逐字相同（仅多 ``projected:`` 注记键）的空文件。"""
    if not description.strip():
        raise ValueError("description is required")
    prefix = files_mod.validate_prefix(name)
    path = files_mod.memory_path(name)
    with files_mod.file_lock(path):
        if path.exists() or _file_row(conn, path.name) is not None:
            raise FileExistsError(f"{path.name} already exists")
        fm = files_mod.default_frontmatter(description=description, tags=tags, status=status)
        file_row = fts.FileRow(
            path=path.name,
            prefix=prefix,
            description=description,
            tags=" ".join(tags),
            status=status,
            entry_count=0,
            created=fm["created"],
            updated=fm["updated"],
            needs_compact=0,
        )
        fts.upsert_file(conn, file_row)
        _project(conn, path=path, nodes=[], file_row=file_row)
    _log.info("created file: %s (status=%s, authority=evomem)", path.name, status)
    return path


def append_entry(
    conn: sqlite3.Connection,
    *,
    name: str,
    content: str,
    tags: list[str],
    soft_limit_tokens: int | None = None,
    confidence: str | None = None,
    conflicted: bool = False,
    occurred_at: str | None = None,
) -> str:
    """evomem 主写的 append：ADD 真相写 + FTS 投影 + 增量 markdown 投影。"""
    from ..store import entries as entries_mod

    path = files_mod.memory_path(name)
    prefix = files_mod.validate_prefix(path.name)
    row = _file_row(conn, path.name)
    if row is None:
        if not path.exists():
            raise FileNotFoundError(f"{path.name} does not exist; call create_file first")
        # 过渡宽容：盘上有文件但 files 行缺失（手建/历史遗留）——从 frontmatter
        # 补行，镜像 legacy「能 append 任何存在的文件」的契约。
        parsed = files_mod.read_file(path)
        # 但若盘上文件已有条目、而 evo_nodes 里没有它的任何节点（未 backfill），直接
        # append 会让 _finish_file_write 的整文件重投影把盘上历史条目全部抹掉（#575）。
        # 拒绝写入并指向 backfill，而不是静默丢数据（CLAUDE.md 也要求反转前先 backfill）。
        if parsed.entries and not _load_file_nodes(conn, path.name):
            raise RuntimeError(
                f"{path.name} 有 {len(parsed.entries)} 条盘上条目但 evo_nodes 无其节点（未"
                f" backfill）；先跑 `persome evomem-backfill` 再 append，"
                f"否则整文件重投影会覆盖历史条目"
            )
        fts.upsert_file(
            conn,
            fts.FileRow(
                path=path.name,
                prefix=prefix,
                description=parsed.description,
                tags=" ".join(parsed.tags),
                status=parsed.status,
                entry_count=len(parsed.entries),
                created=parsed.created,
                updated=parsed.updated,
                needs_compact=1 if parsed.needs_compact else 0,
            ),
        )

    occurred_at = entries_mod._norm_occurred_at(occurred_at)
    ts = entries_mod._now_iso_minute()
    entry_id = entries_mod.make_id(ts)
    all_tags = list(tags) + entries_mod._metadata_tags(confidence, conflicted, occurred_at)
    body = content.strip()
    node = _node_from_write(
        entry_id=entry_id,
        ts=ts,
        tags=all_tags,
        body=body,
        path_name=path.name,
        prefix=prefix,
        supersedes=[],
        confidence=confidence,
        conflicted=conflicted,
        occurred_at=occurred_at,
        valid_from=ts,
    )
    with files_mod.file_lock(path):
        _engine().commit_node(node)  # 真相写（单事务原子）
        entries_mod.derived_append_rows(
            conn,
            entry_id=entry_id,
            path_name=path.name,
            prefix=prefix,
            ts=ts,
            tags_str=" ".join(all_tags),
            content=body,
            confidence=confidence,
            conflicted=conflicted,
            occurred_at=occurred_at,
        )
        _finish_file_write(conn, path=path, prefix=prefix, soft_limit_tokens=soft_limit_tokens)
    return entry_id


def supersede_entry(
    conn: sqlite3.Connection,
    *,
    name: str,
    old_entry_id: str,
    new_content: str,
    reason: str,
    tags: list[str] | None = None,
    refined_from: str | None = None,
    confidence: str | None = None,
    conflicted: bool = False,
    occurred_at: str | None = None,
) -> str:
    """evomem 主写的 supersede：原子「落新链头 + 退役旧节点（双向指针 +
    valid_until）」+ FTS 投影 + markdown 投影。

    语义映射说明（设计稿 §1.3 的实现取舍）：legacy ``refined_from`` 形态
    （EVO-02 双标签法——精炼**入链**退役旧版本）原样保留为「带 ``refined_from``
    出处的 SUPERSEDE 节点」，与 backfill 对既有 markdown 同形态的映射逐字一致；
    engine 的离链 UPDATE op 是 reconcile 决策的 op 级语义，与本写口动词正交。
    新节点 content 携带 legacy 的 ``<!-- supersedes: ...; reason: ... -->``
    机器注释（markdown 投影逐字兼容的一部分）；entries FTS 行的 content 与
    legacy 增量路径一致**不含**注释。

    ``tags=None`` 回退的元认知承继（与 legacy 的一处**有意收敛**）：legacy 把旧
    heading 的原始 tag 集（含 ``confidence:``/``conflicted``/``occurred:``
    colon-tag）原样抄上新 heading，但 ``entry_metadata`` 行不写——文件与派生表
    分裂，直到下次 rebuild 才补上。节点模型没有「heading 有 tag 但列没值」的
    表达，所以反转写口把承继落在 canonical 家（节点列 + ``entry_metadata`` 行），
    heading 由列再渲染：**文件字节与 legacy 一致**（生产站点无 refined_from +
    回退并用的形态），派生表则直接落在 rebuild 后的稳定态——这是修复分裂，
    不是漂移。
    """
    from ..store import entries as entries_mod

    path = files_mod.memory_path(name)
    prefix = files_mod.validate_prefix(path.name)
    if _file_row(conn, path.name) is None and not path.exists():
        raise FileNotFoundError(path.name)
    engine = _engine()
    old_node = engine.store.get(old_entry_id)
    if old_node is None or old_node.file_name != path.name:
        raise ValueError(f"entry {old_entry_id} not found in {path.name}")

    occurred_at = entries_mod._norm_occurred_at(occurred_at)
    ts = entries_mod._now_iso_minute()
    new_id = entries_mod.make_id(ts)
    if tags is None:
        # legacy 回退 = 继承旧条目的 tag 集；语义 tag 走 node.tags，元认知走列
        # （见 docstring「元认知承继」）。
        new_tags = old_node.tags.split()
        if confidence is None and not conflicted and occurred_at is None:
            confidence = old_node.confidence
            conflicted = old_node.conflicted
            occurred_at = old_node.occurred_at
    else:
        new_tags = list(tags)
    if refined_from:
        new_tags.append(f"refined-from:{refined_from}")
    new_tags += entries_mod._metadata_tags(confidence, conflicted, occurred_at)

    body = new_content.strip()
    content_md = f"{body}\n<!-- supersedes: {old_entry_id}; reason: {reason} -->"
    node = _node_from_write(
        entry_id=new_id,
        ts=ts,
        tags=new_tags,
        body=content_md,
        path_name=path.name,
        prefix=prefix,
        supersedes=[old_entry_id],
        confidence=confidence,
        conflicted=conflicted,
        occurred_at=occurred_at,
        valid_from=ts,
    )
    with files_mod.file_lock(path):
        engine.commit_supersede(node, old_id=old_entry_id, old_valid_until=ts)
        entries_mod.derived_supersede_rows(
            conn,
            old_entry_id=old_entry_id,
            new_entry_id=new_id,
            path_name=path.name,
            prefix=prefix,
            ts=ts,
            tags_str=" ".join(new_tags),
            content=body,
            confidence=confidence,
            conflicted=conflicted,
            occurred_at=occurred_at,
        )
        _finish_file_write(conn, path=path, prefix=prefix)
    return new_id


def mark_entry_deleted(conn: sqlite3.Connection, *, name: str, entry_id: str) -> None:
    """evomem 主写的孤儿退役（DELETE / ABSTRACT 源）：shadow + valid_until +
    FTS 退役行 + markdown 投影（已退役条目的重复退役与 legacy 一样不动文件）。"""
    from ..store import entries as entries_mod
    from .models import MemoryStatus

    path = files_mod.memory_path(name)
    prefix = files_mod.validate_prefix(path.name)
    if _file_row(conn, path.name) is None and not path.exists():
        raise FileNotFoundError(path.name)
    engine = _engine()
    node = engine.store.get(entry_id)
    if node is None or node.file_name != path.name:
        raise ValueError(f"entry {entry_id} not found in {path.name}")

    was_active = node.status is MemoryStatus.ACTIVE
    ts = entries_mod._now_iso_minute()
    with files_mod.file_lock(path):
        engine.commit_retire(entry_id, valid_until=ts)
        entries_mod.derived_retire_rows(conn, entry_id=entry_id, ts=ts)
        if was_active:
            # legacy 仅在条目尚未退役时改文件 + 刷 files 行（幂等重退役不产生
            # 文件写）；投影同口径。
            _finish_file_write(conn, path=path, prefix=prefix)


def set_file_status(conn: sqlite3.Connection, *, name: str, status: str) -> None:
    """evomem 主写的文件状态翻转：files 行是真相，投影把它带回 frontmatter。"""
    path = files_mod.memory_path(name)
    if _file_row(conn, path.name) is None:
        return  # 镜像 legacy：文件不存在 = no-op
    conn.execute("UPDATE files SET status = ? WHERE path = ?", (status, path.name))
    with files_mod.file_lock(path):
        reproject_file(conn, path.name)
    _log.info("file status set: %s -> %s (authority=evomem)", path.name, status)


def flag_needs_compact(conn: sqlite3.Connection, *, name: str, value: bool) -> None:
    """evomem 主写的 needs_compact 翻转（``writer/tools.py:tool_flag_compact``
    在反转模式下的形态）：files 行是真相，重投影替代 ``update_frontmatter``。"""
    path = files_mod.memory_path(name)
    fts.set_needs_compact(conn, path.name, value)
    with files_mod.file_lock(path):
        reproject_file(conn, path.name)


def reproject_file(conn: sqlite3.Connection, path_name: str) -> None:
    """按当前真相态（evo_nodes + files 行）重投影单个文件。best-effort。"""
    path = files_mod.memory_path(path_name)
    row = _file_row(conn, path.name)
    if row is None:
        return
    nodes = _load_file_nodes(conn, path.name)
    _project(conn, path=path, nodes=nodes, file_row=row)


# ── 手改检测 + 回灌（Q1 裁定 (b)，PR-6b）────────────────────────────────────


@dataclasses.dataclass
class ImportReport:
    """一次 ``import_markdown_file`` 的结果。"""

    file_name: str
    imported: list[str] = dataclasses.field(default_factory=list)
    conflicts: list[str] = dataclasses.field(default_factory=list)
    reprojected: bool = False


def check_manual_edits(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """对照 ``projection_state``，找出被手改/手删的投影文件（Q1 (b)）。

    判定 = 「当前文件内容 hash ≠ 上次成功投影的 hash」。投影失败不更新 state，
    所以投影滞后不会被误判成手改（lag 走 ``markdown_projection_lag`` 计数器）。
    发现手改只报警（一条 ``integrity_alert`` check=``manual_edit_detected``，
    alert-only）+ 返回清单——**不做自动回灌**（Q1c 被否：mtime 自动回灌等于
    保留第二写口），回灌由人跑 ``persome evomem-import-markdown <file>``。
    """
    findings: list[dict[str, str]] = []
    rows = conn.execute("SELECT file_name, content_hash FROM projection_state").fetchall()
    for r in rows:
        path = files_mod.memory_path(r["file_name"])
        if not path.exists():
            findings.append({"file": r["file_name"], "kind": "missing"})
            continue
        if content_hash(path.read_text()) != r["content_hash"]:
            findings.append({"file": r["file_name"], "kind": "modified"})
    if findings:
        detail = ", ".join(f"{f['file']}({f['kind']})" for f in findings)
        try:
            integrity.emit_alert(
                "manual_edit_detected",
                f"{len(findings)} projection file(s) differ from last projected state: {detail}"
                " — markdown 现在是投影，手改会被下次投影覆盖；"
                "回灌用 `persome evomem-import-markdown <file>`",
                source="write_inversion",
                structural=False,
            )
        except Exception:  # noqa: BLE001
            _log.warning("manual_edit_detected alert emission failed", exc_info=True)
    return findings


def run_daily_manual_edit_check() -> list[dict[str, str]]:
    """daily-safety-net 尾部的挂载形态：仅 evomem 主写时有意义；绝不抛出。"""
    try:
        if not evomem_active():
            return []
        with fts.cursor() as conn:
            findings = check_manual_edits(conn)
        if findings:
            _log.warning("manual-edit check: %d finding(s)", len(findings))
        else:
            _log.info("manual-edit check: clean")
        return findings
    except Exception:  # noqa: BLE001
        _log.warning("manual-edit check failed", exc_info=True)
        return []


def import_markdown_file(conn: sqlite3.Connection, name: str) -> ImportReport:
    """把投影文件里的手改回灌成 engine 写（Q1 (b) 的 import CLI 实现）。

    最小实现（设计任务书裁定）：**只回灌纯新增条目**（无链指针/出处 tag、未退役
    的 ADD 形态，经 ``rebuild_nodes_from_projection`` 同一映射 → ``commit_node``
    + 共享派生行）；带链语义的新增、对既有条目的改动/删除等复杂 diff 一律列入
    ``conflicts`` 报告人裁决，文件**不被重投影覆盖**（保住用户的字，状态 hash 也
    不刷新——手改警报会持续，直到人处理）。全部干净回灌时按 canonical 形态重投影
    并刷新 ``projection_state``。
    """
    from ..store import entries as entries_mod
    from ..store import projector
    from .models import MemoryStatus

    if not evomem_active():
        raise RuntimeError(
            'evomem-import-markdown 仅在 write_authority="evomem" 下有意义；'
            "markdown 主写模式直接编辑文件 + `persome rebuild-index` 即可"
        )
    path = files_mod.memory_path(name)
    if "/" in name or files_mod.validate_prefix(path.name) == "event":
        raise ValueError(f"{path.name} 属豁免口（event-*/子目录），不在投影/回灌范围")
    if not path.exists():
        raise FileNotFoundError(path.name)

    report = ImportReport(file_name=path.name)
    prefix = files_mod.validate_prefix(path.name)
    parsed = files_mod.read_file(path)
    with files_mod.file_lock(path):
        existing = {n.node_id for n in _load_file_nodes(conn, path.name)}
        candidates = {
            n.node_id: n
            for n in projector.rebuild_nodes_from_projection([(path.name, parsed.entries)])
        }
        engine = _engine()
        for e in parsed.entries:
            if e.id in existing:
                continue
            node = candidates[e.id]
            if (
                e.superseded_by
                or e.refined_from
                or e.abstracted_from
                or node.supersedes
                or node.status is not MemoryStatus.ACTIVE
            ):
                report.conflicts.append(f"{e.id}: 新增条目带链指针/退役形态，需人工裁决")
                continue
            node.file_name = path.name
            engine.commit_node(node)
            entries_mod.derived_append_rows(
                conn,
                entry_id=e.id,
                path_name=path.name,
                prefix=prefix,
                ts=e.timestamp,
                tags_str=" ".join(e.tags),
                content=node.content,
                confidence=node.confidence,
                conflicted=node.conflicted,
                occurred_at=node.occurred_at,
            )
            report.imported.append(e.id)

        # 收尾：files 行以真相侧节点数为准；canonical 重投影只在没有残留人裁决
        # 项、且文件内容与 canonical 完全对账时执行（防止覆盖用户改动）。
        nodes = _load_file_nodes(conn, path.name)
        row = _file_row(conn, path.name)
        if row is not None and report.imported:
            file_row = fts.FileRow(
                path=path.name,
                prefix=prefix,
                description=row["description"] or "",
                tags=row["tags"] or "",
                status=row["status"] or "active",
                entry_count=len(nodes),
                created=row["created"] or "",
                updated=files_mod.today(),
                needs_compact=int(row["needs_compact"] or 0),
            )
            fts.upsert_file(conn, file_row)
            row = _file_row(conn, path.name)
        canonical = projector.render_projection(
            path.name, nodes, file_row=_row_mapping(row) if row is not None else None, marker=True
        )
        leftover = {e.id for e in parsed.entries} - {n.node_id for n in nodes}
        if leftover:
            report.conflicts.append(f"未入真相表的条目残留: {', '.join(sorted(leftover))}")
        if not report.conflicts:
            current = path.read_text()
            if _entries_only(current) == _entries_only(canonical):
                files_mod.atomic_write_text(path, canonical)
                record_projection_state(conn, path.name, canonical)
                report.reprojected = True
            else:
                report.conflicts.append(
                    "回灌后文件与 canonical 投影仍有差异（改动了既有条目/无法解析"
                    "的文本？），文件未被覆盖——需人工裁决"
                )
    return report


def _entries_only(text: str) -> list[tuple[str, list[str], str]]:
    """按 entry 粒度归一比较（heading tag 集合序 + body），忽略 frontmatter 与
    块间空行的格式差——手写 append 的格式噪音不应该挡住干净回灌。"""
    try:
        post_body = text.split("---", 2)[2] if text.startswith("---") else text
    except IndexError:
        post_body = text
    # 按 id 排序：canonical 投影按 (heading_ts, id) 重排，手写 append 常落在文件
    # 尾部——条目集合相同而顺序不同不算残留差异。
    return sorted((e.id, e.tags, e.body.strip()) for e in files_mod._parse_entries(post_body))


def project_live_all(conn: sqlite3.Connection) -> list[str]:
    """全量 live 投影：把每个非豁免文件按真相态重投影进 memory/（幂等）。

    服务两个场景：投影滞后修复（``markdown_projection_lag`` 报警后补齐）与 §6
    回滚前置（翻回 markdown 前先把反转期写入投影齐，再 ``rebuild-index``）。
    逐文件 best-effort（坏一个不挡其余），失败走同一 miss 计数。
    """
    done: list[str] = []
    for r in conn.execute("SELECT path FROM files ORDER BY path").fetchall():
        name = r["path"]
        try:
            if "/" in name or files_mod.validate_prefix(name) == "event":
                continue
        except ValueError:
            continue
        reproject_file(conn, name)
        done.append(name)
    return done
