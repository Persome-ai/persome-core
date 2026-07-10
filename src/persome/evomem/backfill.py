"""evo_nodes backfill — 从 markdown + 旁挂表一次性回填（§4.1，PR-2；PR-7 后定型）。

Backfills evomem from the durable Markdown projection.

PR-7 之后的定位：markdown 主写模式（write_authority="markdown"，§6 回滚杠杆）下
evo_nodes 由影子写保鲜；影子滞后（shadow_write_lag 报警 / compact 整文件重写）时
重跑本幂等命令补齐。entry_chain 已退役——``is_latest`` 不再直抄该表，统一由
markdown tag 三态判定派生（与 rebuild 同一判定，原本就是该表的数据源）。

开放问题裁定（同步记录在 PR 描述）：

- **Q2**：``event-*.md`` 豁免不进 evo_nodes——量大、append-only、永不入链，进表只有
  成本没有收益。backfill 跳过 ``event-`` 前缀条目；``integrity._check_evo_projection``
  的对账侧同步排除。
- **Q4**：scope 全取 default——``(user_id, agent_id)`` 用现行默认值。
- **Q8**：``entry_temporal`` 收编进 evo_nodes 的 ``valid_from``/``valid_until`` 列；
  ``entry_retrieval_stats`` 是行为统计不是记忆真相，原样不动。

映射（§4.1）：

- ``node_id = entry_id``（三套 id 空间天然合一）。
- layer 由文件前缀映射；``schema-`` 条目额外从 ``render_schema_body`` 的体例解析回
  四元组（central 留在 content 全文里，summary/inferences/confidence 进三列）。
- status：有后继 → shadow；孤儿 strike → shadow；否则 active——即
  ``store/entries.py:_superseded_from_tags`` 的三态判定原样复用（refined-from 强制活跃）；
  ``is_latest`` 与之同源（``not superseded``）。
- 双向指针由 ``#superseded-by`` 单向 tag 反向连边（back-map 升维到双向）。
- markdown 标签解析全部复用 ``store/files.py:_parse_entries``（ParsedEntry 已携带
  superseded-by/refined-from/abstracted-from/元认知三件套），不重写解析器。

纪律：

- **幂等可重跑**：节点经 INSERT OR REPLACE upsert（NodeStore 的 ON CONFLICT 路径），
  重跑产出 byte-identical 行。
- **执行前快照**（§3.2 变更前快照纪律）：写库前先 ``VACUUM INTO``（复用 PR-1
  ``backup.create_snapshot`` 的验证式快照），快照失败立即中止——不在无救生艇状态下
  搬运真相。
- **收尾断言**：PR-1 ``integrity.run_checks`` 全套自检 + 「evo_nodes 活跃头集合 ==
  entries.superseded=0 集合（排除 event-，FTS 检索投影侧）」逐 id 全等；任一失败 →
  ``ok=False``，CLI 退出非零并报告 diff。
"""

from __future__ import annotations

import contextlib
import dataclasses
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..logger import get
from ..store import entries as entries_mod
from ..store import files as files_mod
from ..store import fts
from . import backup, integrity
from .models import MemoryLayer, MemoryNode, MemoryStatus
from .store import NodeStore

_log = get("persome.evomem")


class BackfillError(RuntimeError):
    """Raised when the backfill must abort before touching evo_nodes."""


# §4.1 前缀 → MemoryLayer。event- 不在表里（Q2 豁免，根本不进映射）；workflow- 当前
# 不在 VALID_PREFIXES，但设计稿点名了它，先占位以免未来加前缀时静默落到兜底层。
_LAYER_BY_PREFIX: dict[str, MemoryLayer] = {
    "user": MemoryLayer.L4_IDENTITY,
    "person": MemoryLayer.L4_IDENTITY,
    "org": MemoryLayer.L4_IDENTITY,
    "project": MemoryLayer.L2_FACT,
    "topic": MemoryLayer.L2_FACT,
    "tool": MemoryLayer.L2_FACT,
    "schema": MemoryLayer.L6_SCHEMA,
    "intent": MemoryLayer.L7_INTENTION,
    "skill": MemoryLayer.L5_KNOWLEDGE,
    "workflow": MemoryLayer.L5_KNOWLEDGE,
}

# 链/出处/元认知 colon-tag 不进 tags 列（§1.2：链由指针列表达，元认知有专列；
# 投影渲染时从列再生成）。其余语义 tag（#schema #stable 等）原样保留。
# 第二组是 markdown 投影生成器（store/projector.py，§1.5「损失趋零」）的附加
# 编码 tag 命名空间——现行格式表达不了的 SSOT 字段（status 的 shadow/archived
# 区分、layer 精确值、非 default scope、temporal）仅在偏离可推导默认值时落
# heading；live markdown（非投影产物）从不携带它们，故对现行 backfill/影子写
# 是死分支，纯加性。
_ENCODED_TAG_PREFIXES = (
    "superseded-by:",
    "refined-from:",
    "abstracted-from:",
    "confidence:",
    "occurred:",
    # —— 投影命名空间（§1.5/§3.4，逆向重建由 projector.rebuild_nodes_from_projection 消费）
    "layer:",
    "status:",
    "scope:",
    "valid-from:",
    "valid-until:",
)


@dataclass
class BackfillReport:
    """One backfill run's outcome — counts, the closing-assertion verdict, diffs."""

    dry_run: bool
    files: int = 0
    scanned_entries: int = 0
    backfilled_nodes: int = 0
    skipped_event: int = 0
    dangling_edges: list[str] = field(default_factory=list)
    violations: list[integrity.Violation] = field(default_factory=list)
    heads_only_evo: list[str] = field(default_factory=list)
    heads_only_fts: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations and not self.heads_only_evo and not self.heads_only_fts


def _parse_minute_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _semantic_tags(tags: list[str]) -> str:
    keep = [t for t in tags if t != "conflicted" and not t.startswith(_ENCODED_TAG_PREFIXES)]
    return " ".join(keep)


def _schema_fields(
    entry: files_mod.ParsedEntry,
) -> tuple[str | None, list[str] | None, float | None]:
    """Parse the L6 四元组 back out of a ``schema-*`` entry (§1.2 / 审计 3.3).

    Body layout is ``render_schema_body``'s: ``central:`` / ``summary:`` one-liners
    then ``inferences:`` bullets (the bullets are pulled by the canonical inverse
    ``parse_expected_inferences``). The miner rides confidence as a float
    ``#confidence:0.72`` heading tag — distinct from the entry-level
    high/medium/low vocabulary, hence parsed as float here and left out of
    ``entry_metadata`` by ``_norm_confidence`` over there.
    """
    # Local import: writer pulls in the LLM stack; only schema entries need this.
    from ..writer.schema_miner_stage import parse_expected_inferences

    summary: str | None = None
    for raw in entry.body.splitlines():
        line = raw.strip()
        if line.lower().startswith("summary:"):
            summary = line.split(":", 1)[1].strip() or None
            break
    inferences = parse_expected_inferences(entry.body)
    confidence: float | None = None
    for t in entry.tags:
        if t.startswith("confidence:"):
            # ValueError = entry-level high/medium/low vocabulary, not the miner float.
            with contextlib.suppress(ValueError):
                confidence = float(t.split(":", 1)[1])
    return summary, inferences, confidence


def map_entry_to_node(
    e: files_mod.ParsedEntry,
    *,
    file_name: str,
    prefix: str,
    supersedes: list[str],
    superseded_by: list[str],
    meta: sqlite3.Row | Mapping[str, Any] | None,
    temporal: sqlite3.Row | Mapping[str, Any] | None,
    user_id: str,
    agent_id: str,
) -> MemoryNode:
    """单条 ParsedEntry → MemoryNode 的共享映射（§4.1 / §4.2 / §3.4）。

    全量 backfill（本模块）与增量影子写（``evomem/shadow.py``，PR-3）共用本函数，
    是「增量影子写后的 evo_nodes 态 == 重跑全量 backfill 的态」核心不变式的实现
    根基——映射逻辑只存在一份，两条路径不可能各自漂移。markdown 投影的逆向重建
    （``store/projector.py:rebuild_nodes_from_projection``，§3.4 灾难恢复工具的
    核心映射）同样以本函数为底，``meta``/``temporal`` 因此放宽接受 Mapping（旁挂
    表行的字典形态，由投影 tag/默认值推得）。

    ``is_latest`` 由 markdown tag 三态判定派生（``not superseded``）——entry_chain
    退役（PR-7）后这是唯一判定；该表当年的行本就由同一判定重放，无信息损失。
    """
    superseded = entries_mod._superseded_from_tags(e)
    content = entries_mod._strip_strike(e.body) if superseded else e.body
    chain_is_latest = 0 if superseded else 1
    ts = _parse_minute_iso(e.timestamp)
    schema_summary = schema_inferences = schema_confidence = None
    if prefix == "schema":
        # 四元组从**去 strike 的** body 解析（PR-6b 修复）：已退役 schema 条目的
        # body 包着 ``~~…~~``，按原文解析会把尾部 ``~~`` 泄进最后一条 inference
        # （首行 ``~~central:`` 同理解析失败）。content 上面已经 strip 过，复用之。
        schema_summary, schema_inferences, schema_confidence = _schema_fields(
            dataclasses.replace(e, body=content)
        )
    return MemoryNode(
        node_id=e.id,
        content=content,
        layer=_LAYER_BY_PREFIX.get(prefix, MemoryLayer.L2_FACT),
        supersedes=sorted(supersedes),
        superseded_by=list(superseded_by),
        is_latest=bool(chain_is_latest),
        status=MemoryStatus.SHADOW if superseded else MemoryStatus.ACTIVE,
        memory_at=ts,
        gmt_created=ts,
        user_id=user_id,
        agent_id=agent_id,
        file_name=file_name,
        tags=_semantic_tags(e.tags),
        refined_from=e.refined_from,
        abstracted_from=list(e.abstracted_from),
        confidence=meta["confidence"] if meta else None,
        conflicted=bool(meta["conflicted"]) if meta else False,
        occurred_at=meta["occurred_at"] if meta else None,
        schema_summary=schema_summary,
        schema_inferences=schema_inferences,
        schema_confidence=schema_confidence,
        valid_from=temporal["valid_from"] if temporal else None,
        valid_until=temporal["valid_until"] if temporal else None,
    )


def _load_side_tables(
    conn: sqlite3.Connection,
) -> tuple[dict[str, sqlite3.Row], dict[str, sqlite3.Row]]:
    metadata = {
        r["entry_id"]: r
        for r in conn.execute(
            "SELECT entry_id, confidence, conflicted, occurred_at FROM entry_metadata"
        )
    }
    temporal = {
        r["entry_id"]: r
        for r in conn.execute("SELECT entry_id, valid_from, valid_until FROM entry_temporal")
    }
    return metadata, temporal


def _build_nodes(report: BackfillReport, *, user_id: str, agent_id: str) -> list[MemoryNode]:
    """Parse markdown + side tables into the full node list (read-only phase)."""
    parsed_files: list[tuple[str, str, list[files_mod.ParsedEntry]]] = []
    for path in files_mod.list_memory_files():
        try:
            prefix = files_mod.validate_prefix(path.name)
        except ValueError as exc:
            _log.warning("backfill: skipping %s: %s", path.name, exc)
            continue
        parsed = files_mod.read_file(path)
        report.files += 1
        report.scanned_entries += len(parsed.entries)
        if prefix == "event":  # Q2 豁免
            report.skipped_event += len(parsed.entries)
            continue
        parsed_files.append((path.name, prefix, parsed.entries))

    with fts.cursor() as conn:
        metadata, temporal = _load_side_tables(conn)

    # 双向指针：#superseded-by 是 old→new 单向 tag；back-map 反向连边升维到双向。
    # 指向未知 id（如已删文件 / event- 豁免区）的边按悬空丢弃并记录——绝不写进
    # 指针列（自检铁律：无悬空 id）。
    known = {e.id for _, _, es in parsed_files for e in es}
    successor_of: dict[str, str] = {}
    for _, _, es in parsed_files:
        for e in es:
            if not e.superseded_by:
                continue
            if e.superseded_by in known:
                successor_of[e.id] = e.superseded_by
            else:
                report.dangling_edges.append(f"{e.id}→{e.superseded_by}")
    supersedes_of: dict[str, list[str]] = {}
    for old_id, new_id in successor_of.items():
        supersedes_of.setdefault(new_id, []).append(old_id)

    nodes: list[MemoryNode] = []
    for file_name, prefix, parsed_entries in parsed_files:
        for e in parsed_entries:
            nodes.append(
                map_entry_to_node(
                    e,
                    file_name=file_name,
                    prefix=prefix,
                    supersedes=supersedes_of.get(e.id, []),
                    superseded_by=[successor_of[e.id]] if e.id in successor_of else [],
                    meta=metadata.get(e.id),
                    temporal=temporal.get(e.id),
                    user_id=user_id,
                    agent_id=agent_id,
                )
            )
    return nodes


def _fts_live_head_ids(conn: sqlite3.Connection) -> set[str]:
    """FTS 检索投影侧的活跃集合：entries.superseded=0，排除 event- 前缀（Q2）。

    收尾对账的对照面（原 entry_chain is_latest=1 集合——P1 不变量
    ``{is_latest=1} ≡ {superseded=0}`` 下两者逐 id 等同，该表退役后改读列）。
    """
    rows = conn.execute("SELECT id FROM entries WHERE superseded=0 AND prefix != 'event'")
    return {r["id"] for r in rows}


def run_backfill(
    *, dry_run: bool = False, user_id: str = "default", agent_id: str = "default"
) -> BackfillReport:
    """One idempotent backfill pass. ``dry_run`` parses/maps/对账 but never writes.

    Raises :class:`BackfillError` when the §3.2 变更前快照 fails (non-dry-run only).
    Closing-assertion failures do NOT raise — they land in the report
    (``violations`` / ``heads_only_*``) with ``ok=False`` so the CLI can print the
    diff and exit non-zero.
    """
    report = BackfillReport(dry_run=dry_run)
    nodes = _build_nodes(report, user_id=user_id, agent_id=agent_id)
    report.backfilled_nodes = len(nodes)
    for edge in report.dangling_edges:
        _log.warning("backfill: dangling #superseded-by edge dropped: %s", edge)

    if not dry_run:
        # §3.2 变更前快照纪律：脚本框架层兜底而非靠人记得。坏快照 = 不动真相。
        # structural_only：投影对账类（check 6）alert-only 发现不否决快照——backfill
        # 正是修齐投影对账的动作，否则带误差的库永远无法靠幂等重跑自愈。
        if backup.create_snapshot(structural_only=True) is None:
            raise BackfillError(
                "pre-backfill snapshot failed (VACUUM INTO / verification) — aborting,"
                " evo_nodes untouched"
            )
        integrity.ensure_writes_allowed()
        store = NodeStore(user_id=user_id, agent_id=agent_id)  # ensures table + migration
        with fts.cursor() as conn:
            conn.execute("BEGIN")
            try:
                for node in nodes:
                    store._upsert_node(conn, node)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    # ── 收尾断言（§4.1）─────────────────────────────────────────────────────
    with fts.cursor() as conn:
        if dry_run:
            evo_heads = {
                n.node_id for n in nodes if n.is_latest and n.status is MemoryStatus.ACTIVE
            }
        else:
            report.violations = integrity.run_checks(conn)
            evo_heads = {
                r["node_id"]
                for r in conn.execute(
                    "SELECT node_id FROM evo_nodes"
                    " WHERE user_id=? AND agent_id=? AND is_latest=1 AND status='active'",
                    (user_id, agent_id),
                )
            }
        fts_heads = _fts_live_head_ids(conn)
    report.heads_only_evo = sorted(evo_heads - fts_heads)
    report.heads_only_fts = sorted(fts_heads - evo_heads)

    _log.info(
        "backfill%s: %d files, %d entries scanned → %d nodes (%d event-* skipped), ok=%s",
        " (dry-run)" if dry_run else "",
        report.files,
        report.scanned_entries,
        report.backfilled_nodes,
        report.skipped_event,
        report.ok,
    )
    return report
