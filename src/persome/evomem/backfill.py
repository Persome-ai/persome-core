"Idempotent backfill from Markdown memory into canonical evomem nodes."

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


_ENCODED_TAG_PREFIXES = (
    "superseded-by:",
    "refined-from:",
    "abstracted-from:",
    "confidence:",
    "occurred:",
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
    superseded = entries_mod._superseded_from_tags(e)
    content = entries_mod._content_from_markdown_entry(e, superseded=superseded)
    chain_is_latest = 0 if superseded else 1
    ts = _parse_minute_iso(e.timestamp)
    schema_summary = schema_inferences = schema_confidence = None
    if prefix == "schema":
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
        if prefix == "event":
            report.skipped_event += len(parsed.entries)
            continue
        parsed_files.append((path.name, prefix, parsed.entries))

    with fts.cursor() as conn:
        metadata, temporal = _load_side_tables(conn)

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
    rows = conn.execute("SELECT id FROM entries WHERE superseded=0 AND prefix != 'event'")
    return {r["id"] for r in rows}


def run_backfill(
    *, dry_run: bool = False, user_id: str = "default", agent_id: str = "default"
) -> BackfillReport:
    report = BackfillReport(dry_run=dry_run)
    nodes = _build_nodes(report, user_id=user_id, agent_id=agent_id)
    report.backfilled_nodes = len(nodes)
    for edge in report.dangling_edges:
        _log.warning("backfill: dangling #superseded-by edge dropped: %s", edge)

    if not dry_run:
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
