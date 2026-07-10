"Deterministic Markdown projection from canonical evomem nodes."

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


PROJECTION_MARKER_KEY = "projected"
PROJECTION_MARKER = "evomem projection; import with evomem-import-markdown"


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
    dt = node.memory_at or node.gmt_created
    if dt is None:
        return _EPOCH_TS
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%dT%H:%M")


def _projects_as_superseded(node: MemoryNode) -> bool:
    if node.superseded_by:
        return True
    if node.refined_from:
        return False
    return node.status is not MemoryStatus.ACTIVE


def _fmt_float(v: float) -> str:
    two = f"{v:.2f}"
    return two if float(two) == v else repr(v)


def _render_tags(node: MemoryNode, *, prefix: str, ts_by_id: Mapping[str, str]) -> list[str]:
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
        if len(node.superseded_by) > 1:
            logger.warning(
                "projector: node %s has %d successors; rendering the first only",
                node.node_id,
                len(node.superseded_by),
            )
        tags.append(f"superseded-by:{node.superseded_by[0]}")

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

    return f"~~{stripped}~~" if stripped else "~~~~"


def _frontmatter_for(
    nodes: list[MemoryNode], file_row: sqlite3.Row | Mapping[str, Any] | None
) -> dict[str, Any]:
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
            "refusing to project into live memory/; this entry point only writes "
            "to an isolated projection directory"
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
