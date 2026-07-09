"""markdown 人读投影生成器（SSOT 切换设计 §1.5，PR-6a）。

evo_nodes（链 SSOT）→ ``memory/*.md`` 形态的人读投影：确定性渲染（按 ``file_name``
分组 → ``(heading_ts, node_id)`` 稳定排序 → 现行 entry 格式），与现行 markdown
写口（``store/entries.py`` 的 ``append_entry``/``supersede_entry``）的输出**逐字
兼容**——这既是 git diff 噪音最小化，也是 §3.4 逆向重建降级路径的前提。

损失趋零（§1.5）：现行格式表达不了的 SSOT 字段以附加 colon-tag 编码进 heading，
且**仅在偏离可推导默认值时**才落 tag（不污染既有形态的字节兼容性）：

- ``#layer:<v>``        当 layer ≠ 文件前缀的默认映射（``backfill._LAYER_BY_PREFIX``）
- ``#status:<v>``       当 status 无法由 strike/superseded-by 形态推导（如
                        refined-from 头被退役——三态判定会强制其活跃）
- ``#scope:<u>/<a>``    当 ``(user_id, agent_id)`` 非 default
- ``#valid-from:<iso>`` 当 ``valid_from`` ≠ heading 时间戳（append 不变式的默认值）
- ``#valid-until:<iso>``当 ``valid_until`` ≠ 后继节点的 heading 时间戳（supersede
                        不变式的默认值；孤儿退役如 DELETE/ABSTRACT 源必落此 tag）

已知有损面（§3.4，诚实标注，round-trip 测试以 writer 可产出的形态钉死其余字段）：

- 时间精度：heading 只有分钟粒度，``memory_at``/``gmt_created`` 的秒级精度丢失；
  逆向重建后两者都取 heading 时间戳。
- ``valid_from`` 为 NULL 的节点逆向重建为 heading 时间戳（append 不变式的语义
  正确值）；engine 直写节点（PR-6a 暂不写 temporal）属此形态。
- 退役 body 内嵌的 ``~~…~~`` 会被 ``_strip_strike`` 一并展开（与现行 rebuild
  行为一致）。
- 未路由节点（``file_name=''``，如 run_system2 直写的 L6 demo 节点）不投影，
  计入 ``skipped_unrouted``。

两种触发（§1.5）：增量（:func:`project_file`，按 file_name 重投影单文件——
**PR-6a 不挂任何生产钩子**，挂钩属 6b 写口反转）+ 全量（:func:`project_all`，
``persome evomem-project-markdown`` CLI，幂等）。输出目录默认
``<root>/projection-md``，**拒绝指向 live memory/**（危险面留给 6b 管控）。

frontmatter 的 files 表元数据（description/tags/status/created/updated/
entry_count/needs_compact）同样由投影器维护：以 ``files`` 表行为基底、
``entry_count`` 以真相侧节点数为准。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frontmatter

from .. import paths
from ..evomem import backfill
from ..evomem import store as evo_store
from ..evomem.models import MemoryLayer, MemoryNode, MemoryStatus
from ..logger import get
from . import files as files_mod
from . import fts

logger = get("persome.store.projector")

_DEFAULT_SCOPE = ("default", "default")
_EPOCH_TS = "1970-01-01T00:00"

# 投影注记（Q1 裁定 (b)，PR-6b）：写权反转后落到 live memory/ 的投影文件在
# frontmatter 带本键，向打开文件的人声明「这是 evo_nodes 的投影，手改会被下次
# 投影覆盖，回灌走 import CLI」。放 frontmatter 而非文件尾 HTML 注释，是因为
# 尾注会被 entry 解析吞进最后一条 entry 的 body，破坏 §3.4 round-trip。
# 默认 False：CI round-trip / projection-md 隔离目录投影保持 6a 的逐字兼容形态。
PROJECTION_MARKER_KEY = "projected"
PROJECTION_MARKER = "evo_nodes — 手改会被投影覆盖; 回灌: persome evomem-import-markdown"

# 投影命名空间 colon-tag（与 backfill._ENCODED_TAG_PREFIXES 的第二组一致；
# 逆向半程由 rebuild_nodes_from_projection 消费）。
_TAG_LAYER = "layer:"
_TAG_STATUS = "status:"
_TAG_SCOPE = "scope:"
_TAG_VALID_FROM = "valid-from:"
_TAG_VALID_UNTIL = "valid-until:"


@dataclass
class ProjectionReport:
    """One full-projection run's outcome."""

    out_dir: Path
    files: list[str] = field(default_factory=list)
    nodes: int = 0
    skipped_unrouted: int = 0


def _heading_ts(node: MemoryNode) -> str:
    """节点的 heading 时间戳（本地分钟，与 ``_now_iso_minute`` 同口径）。"""
    dt = node.memory_at or node.gmt_created
    if dt is None:
        return _EPOCH_TS
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%dT%H:%M")


def _projects_as_superseded(node: MemoryNode) -> bool:
    """forward 版三态判定——必须是 ``entries._superseded_from_tags`` 的镜像。

    superseded-by → 退役；refined-from → 强制活跃（即便已 shadow，此形态由
    ``#status:`` tag 另行编码）；否则按 status 落孤儿 strike。
    """
    if node.superseded_by:
        return True
    if node.refined_from:
        return False
    return node.status is not MemoryStatus.ACTIVE


def _fmt_float(v: float) -> str:
    """schema confidence 浮点 tag：优先 miner 的 ``.2f`` 形态，超精度退 repr。"""
    two = f"{v:.2f}"
    return two if float(two) == v else repr(v)


def _render_tags(node: MemoryNode, *, prefix: str, ts_by_id: Mapping[str, str]) -> list[str]:
    """单节点 heading tag 列表（顺序镜像现行写口的产出形态）。

    现行序：语义 tag → refined-from → abstracted-from → confidence → conflicted
    → occurred →（supersede 时追加在最尾的）superseded-by；投影附加 tag 排在
    其后（live markdown 无此组，字节兼容不受扰）。
    """
    struck = _projects_as_superseded(node)
    tags = list(node.tags.split())
    if node.refined_from:
        tags.append(f"refined-from:{node.refined_from}")
    if node.abstracted_from:
        tags.append("abstracted-from:" + ",".join(node.abstracted_from))
    if node.confidence:
        tags.append(f"confidence:{node.confidence}")
    elif node.schema_confidence is not None:
        tags.append(f"confidence:{_fmt_float(node.schema_confidence)}")
    if node.conflicted:
        tags.append("conflicted")
    if node.occurred_at:
        tags.append(f"occurred:{node.occurred_at}")
    if node.superseded_by:
        # 反分叉铁律（integrity 检查 3）保证 ≤1；异常多后继只渲染首个并告警。
        if len(node.superseded_by) > 1:
            logger.warning(
                "projector: node %s has %d successors; rendering the first only",
                node.node_id,
                len(node.superseded_by),
            )
        tags.append(f"superseded-by:{node.superseded_by[0]}")

    # ── 损失趋零附加 colon-tag（仅在偏离可推导默认值时落）──────────────────
    default_layer = backfill._LAYER_BY_PREFIX.get(prefix, MemoryLayer.L2_FACT)
    if node.layer is not default_layer:
        tags.append(f"{_TAG_LAYER}{node.layer}")
    derived_status = MemoryStatus.SHADOW if struck else MemoryStatus.ACTIVE
    if node.status is not derived_status:
        tags.append(f"{_TAG_STATUS}{node.status}")
    if (node.user_id, node.agent_id) != _DEFAULT_SCOPE:
        tags.append(f"{_TAG_SCOPE}{node.user_id}/{node.agent_id}")
    heading_ts = _heading_ts(node)
    if node.valid_from and node.valid_from != heading_ts:
        tags.append(f"{_TAG_VALID_FROM}{node.valid_from}")
    succ_ts = ts_by_id.get(node.superseded_by[0]) if node.superseded_by else None
    if node.valid_until and node.valid_until != succ_ts:
        tags.append(f"{_TAG_VALID_UNTIL}{node.valid_until}")
    return tags


def _render_body(node: MemoryNode) -> str:
    if not _projects_as_superseded(node):
        return node.content
    stripped = node.content.strip()
    # 空 body 退役与 entries._strike_entry_body 的 ``~~~~`` 哨兵同形态。
    return f"~~{stripped}~~" if stripped else "~~~~"


def _frontmatter_for(
    nodes: list[MemoryNode], file_row: sqlite3.Row | Mapping[str, Any] | None
) -> dict[str, Any]:
    """frontmatter 元数据：以 files 表行为基底，entry_count 以真相侧节点数为准。"""
    dates = [_heading_ts(n)[:10] for n in nodes]
    if file_row is not None:
        return {
            "description": file_row["description"],
            "tags": (file_row["tags"] or "").split(),
            "status": file_row["status"],
            "created": file_row["created"],
            "updated": file_row["updated"],
            "entry_count": len(nodes),
            "needs_compact": bool(file_row["needs_compact"]),
        }
    return {
        "description": "",
        "tags": [],
        "status": "active",
        "created": min(dates) if dates else "",
        "updated": max(dates) if dates else "",
        "entry_count": len(nodes),
        "needs_compact": False,
    }


def render_content(file_name: str, nodes: Iterable[MemoryNode]) -> str:
    """确定性渲染一个 file_name 分组的 entry 正文（不含 frontmatter）。

    单独成函数（PR-6b）：反转写口的 soft-limit 估算（``len(content)//4``，与
    markdown 主写的 ``post.content`` 同口径）只需要正文，不需要 frontmatter。
    """
    prefix = files_mod.validate_prefix(file_name)
    ordered = sorted(nodes, key=lambda n: (_heading_ts(n), n.node_id))
    ts_by_id = {n.node_id: _heading_ts(n) for n in ordered}
    content = ""
    for i, n in enumerate(ordered):
        heading = files_mod.render_heading(
            timestamp=_heading_ts(n),
            entry_id=n.node_id,
            tags=_render_tags(n, prefix=prefix, ts_by_id=ts_by_id),
        )
        body = _render_body(n)
        block = f"{heading}\n{body}" if body else heading
        if i:
            # 现行写口的间隔形态：append_entry 先 rstrip 再接 "\n\n"（一个空行），
            # supersede_entry 在保留尾 "\n" 的原文上追加 "\n\n"（两个空行）。链上
            # 带 supersedes 指针的节点 ⟺ supersede_entry 产物，镜像之以保证逐字
            # 兼容（git diff 噪音最小化，§1.5）。
            content += "\n\n\n" if n.supersedes else "\n\n"
        content += block
    return content


def render_projection(
    file_name: str,
    nodes: Iterable[MemoryNode],
    *,
    file_row: sqlite3.Row | Mapping[str, Any] | None = None,
    marker: bool = False,
) -> str:
    """确定性渲染一个 file_name 分组为完整 markdown 文本（含 frontmatter）。

    排序键 ``(heading_ts, node_id)``——heading 时间戳是分钟级字符串，同分钟内按
    node_id（``make_id`` 形态自带日期-时间前缀）稳定定序；重复渲染逐字节幂等。
    最终落字节走与 ``append_entry`` 同一条 ``frontmatter.dumps(post) + "\\n"``
    表达式，frontmatter 键序/引号形态由同一库产生，保证逐字兼容。

    ``marker=True``（PR-6b 反转写口专用）：frontmatter 追加
    ``PROJECTION_MARKER_KEY`` 注记键。默认 False 保持 6a 的逐字兼容产出。
    """
    ordered = sorted(nodes, key=lambda n: (_heading_ts(n), n.node_id))
    content = render_content(file_name, ordered)
    meta = _frontmatter_for(ordered, file_row)
    if marker:
        meta[PROJECTION_MARKER_KEY] = PROJECTION_MARKER
    post = frontmatter.Post(content=content, **meta)
    rendered: str = frontmatter.dumps(post)
    return rendered + "\n"


def _guard_out_dir(out_dir: Path) -> Path:
    out = Path(out_dir)
    if out.resolve() == paths.memory_dir().resolve():
        raise ValueError(
            "refusing to project into live memory/ — 投影覆盖真相目录的危险面"
            "属 PR-6b（写口反转）管控，本入口只写隔离目录"
        )
    return out


def _load_nodes(
    conn: sqlite3.Connection, *, user_id: str, agent_id: str, file_name: str | None = None
) -> list[MemoryNode]:
    sql = "SELECT * FROM evo_nodes WHERE user_id=? AND agent_id=?"
    args: list[str] = [user_id, agent_id]
    if file_name is not None:
        sql += " AND file_name=?"
        args.append(file_name)
    rows = conn.execute(sql + " ORDER BY node_id", args).fetchall()
    return [evo_store._row_to_node(r) for r in rows]


def _file_row(conn: sqlite3.Connection, file_name: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM files WHERE path=?", (file_name,)
    ).fetchone()
    return row


def project_file(
    conn: sqlite3.Connection,
    file_name: str,
    *,
    out_dir: Path,
    user_id: str = "default",
    agent_id: str = "default",
) -> Path:
    """增量触发形态：按 ``file_name`` 重投影单个文件（§1.5）。

    **PR-6a 不挂生产钩子**——engine 写后自动重投影属 6b；本函数当前只有 CLI
    （``--file``）与测试调用。写出走 ``atomic_write_text``，幂等。
    """
    out = _guard_out_dir(out_dir)
    nodes = _load_nodes(conn, user_id=user_id, agent_id=agent_id, file_name=file_name)
    text = render_projection(file_name, nodes, file_row=_file_row(conn, file_name))
    target = out / file_name
    files_mod.atomic_write_text(target, text)
    return target


def project_all(
    conn: sqlite3.Connection,
    *,
    out_dir: Path,
    user_id: str = "default",
    agent_id: str = "default",
) -> ProjectionReport:
    """全量投影（§1.5）：evo_nodes 按 file_name 分组逐文件渲染进 ``out_dir``。幂等。"""
    out = _guard_out_dir(out_dir)
    report = ProjectionReport(out_dir=out)
    groups: dict[str, list[MemoryNode]] = {}
    for node in _load_nodes(conn, user_id=user_id, agent_id=agent_id):
        if not node.file_name:
            report.skipped_unrouted += 1
            continue
        groups.setdefault(node.file_name, []).append(node)
    for file_name in sorted(groups):
        nodes = groups[file_name]
        text = render_projection(file_name, nodes, file_row=_file_row(conn, file_name))
        files_mod.atomic_write_text(out / file_name, text)
        report.files.append(file_name)
        report.nodes += len(nodes)
    logger.info(
        "projected %d file(s), %d node(s) → %s (%d unrouted node(s) skipped)",
        len(report.files),
        report.nodes,
        out,
        report.skipped_unrouted,
    )
    return report


# ── 逆向半程（§3.4 round-trip CI 的机器守护）────────────────────────────────


def _projection_overrides(tags: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for t in tags:
        for prefix in (_TAG_LAYER, _TAG_STATUS, _TAG_SCOPE, _TAG_VALID_FROM, _TAG_VALID_UNTIL):
            if t.startswith(prefix):
                out[prefix] = t.split(":", 1)[1]
    return out


def rebuild_nodes_from_projection(
    parsed_files: list[tuple[str, list[files_mod.ParsedEntry]]],
) -> list[MemoryNode]:
    """从投影 markdown 逆向重建 MemoryNode 列表（§3.4 的核心映射，round-trip 逆向半程）。

    入参是 ``(file_name, files.read_file(...).entries)``。复用 backfill 的解析
    逻辑：``_superseded_from_tags`` 三态判定、``#superseded-by`` back-map 升维
    双向、元认知 tag 三件套——核心走 ``backfill.map_entry_to_node`` 同一函数，
    再叠加投影命名空间 colon-tag 的还原：

    - ``meta``：从 entry 自身的 heading tag 推得（``fts._norm_confidence`` 同口径，
      schema 浮点 confidence 不会漏进 entry 级列——与旁挂表写口一致）。
    - ``temporal``：``valid-from``/``valid-until`` tag 优先；缺省按写口不变式推导
      （valid_from = heading ts；valid_until = 后继 heading ts / 无后继为 NULL）。
    - ``layer``/``status``/``scope``：tag 覆盖默认推导；status 覆盖时 ``is_latest``
      随 status 派生（writer 可产出的组合里二者从不背离）。

    本函数是 PR-7 ``import_from_markdown`` 灾难恢复工具的前身映射；PR-6a 仅由
    round-trip CI 与逐字兼容性测试消费，无生产调用方。
    """
    known = {e.id for _, es in parsed_files for e in es}
    successor_of: dict[str, str] = {}
    for _, es in parsed_files:
        for e in es:
            if e.superseded_by and e.superseded_by in known:
                successor_of[e.id] = e.superseded_by
    supersedes_of: dict[str, list[str]] = {}
    for old_id, new_id in successor_of.items():
        supersedes_of.setdefault(new_id, []).append(old_id)
    ts_of = {e.id: e.timestamp for _, es in parsed_files for e in es}

    nodes: list[MemoryNode] = []
    for file_name, entries in parsed_files:
        prefix = files_mod.validate_prefix(file_name)
        for e in entries:
            overrides = _projection_overrides(e.tags)
            user_id, agent_id = _DEFAULT_SCOPE
            if _TAG_SCOPE in overrides and "/" in overrides[_TAG_SCOPE]:
                user_id, agent_id = overrides[_TAG_SCOPE].split("/", 1)
            successor = successor_of.get(e.id)
            valid_until = overrides.get(_TAG_VALID_UNTIL) or (
                ts_of[successor] if successor else None
            )
            node = backfill.map_entry_to_node(
                e,
                file_name=file_name,
                prefix=prefix,
                supersedes=supersedes_of.get(e.id, []),
                superseded_by=[successor] if successor else [],
                meta={
                    "confidence": fts._norm_confidence(e.confidence),
                    "conflicted": 1 if e.conflicted else 0,
                    "occurred_at": e.occurred_at,
                },
                temporal={
                    "valid_from": overrides.get(_TAG_VALID_FROM) or e.timestamp,
                    "valid_until": valid_until,
                },
                user_id=user_id,
                agent_id=agent_id,
            )
            if _TAG_LAYER in overrides:
                node.layer = MemoryLayer(overrides[_TAG_LAYER])
            if _TAG_STATUS in overrides:
                node.status = MemoryStatus(overrides[_TAG_STATUS])
                node.is_latest = node.status is MemoryStatus.ACTIVE
            nodes.append(node)
    return nodes
