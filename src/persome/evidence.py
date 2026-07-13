"""Unified, read-only evidence resolution for MCP and the local model viewer.

Receipts are stable pointers, not embedded payloads.  This module resolves the
pointer formats already emitted by memory, evomem, activities, and model
snapshots into one small response contract.  It deliberately labels
time-adjacent captures as context rather than claiming they are direct sources.
"""

from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path
from typing import Any

_SUMMARY_LIMIT = 4_000
_NEARBY_CAPTURE_LIMIT = 3
_NEARBY_CAPTURE_SECONDS = 30 * 60


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _json_list(value: Any) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if item is not None and str(item).strip()]


def _trim(value: Any, limit: int = _SUMMARY_LIMIT) -> str:
    return str(value or "").strip()[:limit]


def _humanize_path(value: str | None) -> str:
    name = str(value or "").rsplit("/", 1)[-1].removesuffix(".md").removesuffix(".json")
    return " ".join(part.capitalize() for part in name.replace("_", "-").split("-") if part)


def _short_label(value: Any, fallback: str, limit: int = 140) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] if text else fallback


def parse_reference(reference: str) -> tuple[str, str | None, str | None]:
    """Return ``(identifier, path_hint, canonical_receipt)``.

    Receipt identifiers can themselves contain colons, so the path separator
    is the final colon inside ``⟨identifier:path⟩``.
    """
    value = str(reference or "").strip()
    if value.startswith("⟨") and value.endswith("⟩"):
        inner = value[1:-1]
        identifier, separator, path_hint = inner.rpartition(":")
        if separator and identifier.strip() and path_hint.strip():
            return identifier.strip(), path_hint.strip(), value
    return value, None, None


def _link(
    *,
    relation: str,
    kind: str,
    identifier: str,
    reference: str | None = None,
    label: str = "",
    timestamp: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    return {
        "relation": relation,
        "kind": kind,
        "id": identifier,
        "reference": reference or identifier,
        "label": label,
        "timestamp": timestamp,
        "status": status,
        "resolvable": True,
    }


def _base(
    *,
    reference: str,
    canonical_reference: str,
    kind: str,
    identifier: str,
    status: str,
    label: str = "",
    summary: str = "",
    timestamp: str | None = None,
    path: str | None = None,
    metadata: dict[str, Any] | None = None,
    sources: list[dict[str, Any]] | None = None,
    context: list[dict[str, Any]] | None = None,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "reference": reference,
        "canonical_reference": canonical_reference,
        "kind": kind,
        "id": identifier,
        "status": status,
        "label": label or identifier,
        "summary": summary,
        "timestamp": timestamp,
        "path": path,
        "metadata": metadata or {},
        "sources": sources or [],
        "context": context or [],
        "history": history or [],
    }


def _evo_reference(conn: sqlite3.Connection, node_id: str) -> tuple[str, str]:
    try:
        row = conn.execute(
            "SELECT file_name, content FROM evo_nodes"
            " WHERE node_id=? ORDER BY is_latest DESC LIMIT 1",
            (node_id,),
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row is None:
        return node_id, node_id
    path = str(row[0] or "")
    label = _short_label(row[1], _humanize_path(path) or node_id)
    return f"⟨{node_id}:{path}⟩", label


def _reference_label(conn: sqlite3.Connection, reference: str) -> str:
    identifier, path_hint, _receipt = parse_reference(reference)
    if _table_exists(conn, "entries"):
        row = conn.execute("SELECT content, path FROM entries WHERE id=?", (identifier,)).fetchone()
        if row is not None:
            return _short_label(row[0], _humanize_path(str(row[1] or "")) or identifier)
    if _table_exists(conn, "evo_nodes"):
        row = conn.execute(
            "SELECT content, file_name FROM evo_nodes"
            " WHERE node_id=? ORDER BY is_latest DESC LIMIT 1",
            (identifier,),
        ).fetchone()
        if row is not None:
            return _short_label(row[0], _humanize_path(str(row[1] or "")) or identifier)
    if _table_exists(conn, "captures"):
        row = conn.execute(
            "SELECT app_name, window_title FROM captures WHERE id=?", (identifier,)
        ).fetchone()
        if row is not None:
            return " · ".join(str(value) for value in row if value) or identifier
    return _humanize_path(path_hint) or identifier


def _nearby_capture_links(conn: sqlite3.Connection, timestamp: str | None) -> list[dict[str, Any]]:
    if not timestamp or not _table_exists(conn, "captures"):
        return []
    try:
        rows = conn.execute(
            "SELECT id, timestamp, app_name, window_title FROM captures"
            " WHERE abs(persome_epoch(timestamp) - persome_epoch(?)) <= ?"
            " ORDER BY abs(persome_epoch(timestamp) - persome_epoch(?)) LIMIT ?",
            (timestamp, _NEARBY_CAPTURE_SECONDS, timestamp, _NEARBY_CAPTURE_LIMIT),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [
        _link(
            relation="nearby_context",
            kind="capture",
            identifier=str(row[0]),
            label=" · ".join(part for part in (str(row[2] or ""), str(row[3] or "")) if part),
            timestamp=str(row[1]) if row[1] else None,
        )
        for row in rows
    ]


def _resolve_entry(
    conn: sqlite3.Connection,
    *,
    original: str,
    identifier: str,
    receipt: str | None,
) -> dict[str, Any] | None:
    if not _table_exists(conn, "entries"):
        return None
    try:
        row = conn.execute(
            "SELECT id, path, timestamp, tags, content, superseded FROM entries WHERE id=?",
            (identifier,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None

    metadata: dict[str, Any] = {
        "tags": str(row[3] or "").split(),
        "superseded": bool(row[5]),
    }
    if _table_exists(conn, "entry_metadata"):
        meta = conn.execute(
            "SELECT confidence, conflicted, occurred_at FROM entry_metadata WHERE entry_id=?",
            (identifier,),
        ).fetchone()
        if meta is not None:
            metadata.update(
                confidence=meta[0],
                conflicted=bool(meta[1]),
                occurred_at=meta[2],
            )
    if _table_exists(conn, "entry_temporal"):
        temporal = conn.execute(
            "SELECT valid_from, valid_until FROM entry_temporal WHERE entry_id=?",
            (identifier,),
        ).fetchone()
        if temporal is not None:
            metadata.update(valid_from=temporal[0], valid_until=temporal[1])

    path = str(row[1] or "")
    canonical = f"⟨{identifier}:{path}⟩" if path else receipt or identifier
    timestamp = str(row[2]) if row[2] else None
    context_timestamp = str(metadata.get("occurred_at") or timestamp or "") or None
    return _base(
        reference=original,
        canonical_reference=canonical,
        kind="memory",
        identifier=identifier,
        status="superseded" if row[5] else "active",
        label=_short_label(row[4], _humanize_path(path) or identifier),
        summary=_trim(row[4]),
        timestamp=timestamp,
        path=path,
        metadata=metadata,
        context=_nearby_capture_links(conn, context_timestamp),
    )


def _resolve_evo_node(
    conn: sqlite3.Connection,
    *,
    original: str,
    identifier: str,
    receipt: str | None,
) -> dict[str, Any] | None:
    if not _table_exists(conn, "evo_nodes"):
        return None
    try:
        row = conn.execute(
            "SELECT node_id, content, layer, supersedes, is_latest, status, memory_at,"
            " gmt_created, file_name, tags, refined_from, abstracted_from, confidence,"
            " conflicted, occurred_at, valid_from, valid_until, superseded_by"
            " FROM evo_nodes WHERE node_id=? ORDER BY is_latest DESC LIMIT 1",
            (identifier,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None

    # Version-chain edges belong in ``history`` below. Direct sources are only
    # derivation inputs (abstraction/refinement), so clients do not present an
    # earlier wording as independent proof for the current wording.
    parent_ids = list(dict.fromkeys(_json_list(row[11])))
    if row[10] and str(row[10]) not in parent_ids:
        parent_ids.append(str(row[10]))
    sources = []
    for parent_id in parent_ids:
        parent_reference, parent_label = _evo_reference(conn, parent_id)
        sources.append(
            _link(
                relation="direct_lineage",
                kind="point",
                identifier=parent_id,
                reference=parent_reference,
                label=parent_label,
            )
        )

    history: list[dict[str, Any]] = []
    for relation, related_ids in (
        ("previous_version", _json_list(row[3])),
        ("next_version", _json_list(row[17])),
    ):
        for related_id in related_ids:
            related_reference, related_label = _evo_reference(conn, related_id)
            history.append(
                _link(
                    relation=relation,
                    kind="point",
                    identifier=related_id,
                    reference=related_reference,
                    label=related_label,
                )
            )

    path = str(row[8] or "")
    canonical = f"⟨{identifier}:{path}⟩" if path else receipt or identifier
    timestamp = str(row[14] or row[6] or row[7]) if row[14] or row[6] or row[7] else None
    return _base(
        reference=original,
        canonical_reference=canonical,
        kind="point",
        identifier=identifier,
        status=str(row[5] or ("active" if row[4] else "superseded")),
        label=_short_label(row[1], _humanize_path(path) or identifier),
        summary=_trim(row[1]),
        timestamp=timestamp,
        path=path,
        metadata={
            "layer": row[2],
            "is_latest": bool(row[4]),
            "tags": str(row[9] or "").split(),
            "confidence": row[12],
            "conflicted": bool(row[13]),
            "occurred_at": row[14],
            "valid_from": row[15],
            "valid_until": row[16],
        },
        sources=sources,
        context=_nearby_capture_links(conn, timestamp),
        history=history,
    )


def _resolve_capture(
    conn: sqlite3.Connection,
    *,
    original: str,
    identifier: str,
) -> dict[str, Any] | None:
    if not _table_exists(conn, "captures"):
        return None
    try:
        row = conn.execute(
            "SELECT id, timestamp, app_name, bundle_id, window_title, focused_role,"
            " focused_value, visible_text, url FROM captures WHERE id=?",
            (identifier,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None

    from . import paths

    # Capture IDs are file stems.  Even if an imported/corrupt database contains
    # a hostile absolute or nested ID, evidence lookup must not turn it into an
    # existence probe outside the capture buffer.
    capture_name = f"{identifier}.json"
    buffer_available = False
    if Path(capture_name).name == capture_name:
        try:
            retained = (paths.capture_buffer_dir() / capture_name).lstat()
            buffer_available = stat.S_ISREG(retained.st_mode) and retained.st_nlink == 1
        except OSError:
            pass
    summary = _trim(row[7] or row[6])
    return _base(
        reference=original,
        canonical_reference=identifier,
        kind="capture",
        identifier=identifier,
        status="available" if buffer_available else "metadata_only",
        label=(
            " · ".join(part for part in (str(row[2] or ""), str(row[4] or "")) if part)
            or identifier
        ),
        summary=summary,
        timestamp=str(row[1]) if row[1] else None,
        metadata={
            "provenance": "observed",
            "app_name": row[2],
            "bundle_id": row[3],
            "window_title": row[4],
            "focused_role": row[5],
            "focused_value": _trim(row[6], 1_000),
            "url": row[8],
            "raw_capture_available": buffer_available,
        },
    )


def _resolve_activity(
    conn: sqlite3.Connection,
    *,
    original: str,
    identifier: str,
    receipt: str | None,
) -> dict[str, Any] | None:
    from .model.activity_source import ActivitySource, is_activity_identity

    _receipt_id, path_hint, _canonical = parse_reference(original)
    if is_activity_identity(identifier):
        activity_identity = identifier
    elif path_hint in {"sessions", "intents"}:
        activity_identity = f"event:{path_hint.removesuffix('s')}:{identifier}"
    else:
        return None
    try:
        event = ActivitySource(conn).event(activity_identity)
    except sqlite3.Error:
        return None
    if event is None:
        return None

    sources: list[dict[str, Any]] = []
    if event.source_kind == "entry" and event.source_receipt != original:
        sources.append(
            _link(
                relation="direct_source",
                kind="memory",
                identifier=event.source_id,
                reference=event.source_receipt,
                label=_reference_label(conn, event.source_receipt),
                timestamp=event.occurred_at,
            )
        )
    return _base(
        reference=original,
        canonical_reference=event.source_receipt,
        kind="activity",
        identifier=event.stable_id,
        status="historical",
        label=_short_label(event.summary, "Activity"),
        summary=_trim(event.summary),
        timestamp=event.occurred_at,
        path=event.source_receipt,
        metadata={
            "source_kind": event.source_kind,
            "source_id": event.source_id,
            "participant_ids": event.participant_ids,
        },
        sources=sources,
        context=_nearby_capture_links(conn, event.occurred_at),
    )


def _receipt_values(item: dict[str, Any]) -> list[str]:
    values = [item.get("receipt")]
    source_evidence = item.get("source_evidence")
    if isinstance(source_evidence, dict):
        values.append(source_evidence.get("receipt"))
    values.extend(item.get("member_receipts") or [])
    values.extend(item.get("source_receipts") or [])
    return list(dict.fromkeys(str(value) for value in values if value))


def _has_geometry_identity(conn: sqlite3.Connection, identifier: str) -> bool:
    """Cheaply reject arbitrary IDs before building the full model snapshot."""
    if identifier.startswith("evolution:"):
        return True
    for table, column in (("relation_edges", "edge_id"), ("schema_faces", "face_id")):
        if not _table_exists(conn, table):
            continue
        try:
            if conn.execute(
                f"SELECT 1 FROM {table} WHERE {column}=? LIMIT 1", (identifier,)
            ).fetchone():
                return True
        except sqlite3.Error:
            continue
    return False


def _resolve_geometry(
    conn: sqlite3.Connection,
    *,
    original: str,
    identifier: str,
) -> dict[str, Any] | None:
    if not _has_geometry_identity(conn, identifier):
        return None

    from .model.snapshot import build_snapshot

    try:
        snapshot = build_snapshot(conn, redact=False)
    except (sqlite3.Error, RuntimeError, TypeError, ValueError):
        # Evidence decoration must stay available even when an old or corrupt
        # model cannot satisfy the current snapshot contract.
        return None
    candidates: list[tuple[str, dict[str, Any]]] = []
    candidates.extend(("point", item) for item in snapshot["points"])
    candidates.extend(("line", item) for item in snapshot["lines"])
    candidates.extend(("face", item) for item in snapshot["faces"])
    candidates.extend(("volume", item) for item in snapshot["volumes"])
    if snapshot["root"] is not None:
        candidates.append(("root", snapshot["root"]))
    match = next(((kind, item) for kind, item in candidates if item.get("id") == identifier), None)
    if match is None:
        return None
    kind, item = match
    references = _receipt_values(item)
    point_labels = {
        str(point["receipt"]): _short_label(
            point.get("content"),
            _humanize_path(str(point.get("file_name") or "")) or str(point.get("id") or "Point"),
        )
        for point in snapshot["points"]
        if point.get("receipt")
    }
    sources = [
        _link(
            relation="direct_evidence",
            kind="receipt",
            identifier=parse_reference(value)[0],
            reference=value,
            label=point_labels.get(value) or _reference_label(conn, value),
        )
        for value in references
    ]
    summary = item.get("content") or item.get("signature") or item.get("label") or identifier
    timestamp = item.get("occurred_at") or item.get("valid_from") or item.get("created_at")
    return _base(
        reference=original,
        canonical_reference=identifier,
        kind=kind,
        identifier=identifier,
        status=str(item.get("status") or "active"),
        label=_short_label(summary, kind.capitalize()),
        summary=_trim(summary),
        timestamp=str(timestamp) if timestamp else None,
        metadata={
            key: item.get(key)
            for key in ("confidence", "observations", "provenance", "valid_from", "members")
            if item.get(key) is not None
        },
        sources=sources,
    )


def resolve_evidence(conn: sqlite3.Connection, reference: str) -> dict[str, Any]:
    """Resolve one receipt or object id into a progressive-disclosure node.

    ``sources`` are explicit lineage already stored by Persome. ``context`` is
    deliberately separate: it contains time-adjacent captures that may help an
    owner investigate, but are not claimed as the inputs that produced the
    derived object.
    """
    original = str(reference or "").strip()
    identifier, path_hint, receipt = parse_reference(original)
    if not identifier:
        return _base(
            reference=original,
            canonical_reference=original,
            kind="unknown",
            identifier="",
            status="missing",
            label="Unknown evidence",
            metadata={"reason": "empty_reference"},
        )

    # A receipt path is a strong type hint. Memory and evomem receipts are
    # checked before generic activities; bare ids use the same deterministic
    # order and then fall through to captures/model geometry.
    resolvers = (
        lambda: _resolve_entry(conn, original=original, identifier=identifier, receipt=receipt),
        lambda: _resolve_evo_node(conn, original=original, identifier=identifier, receipt=receipt),
        lambda: _resolve_activity(conn, original=original, identifier=identifier, receipt=receipt),
        lambda: _resolve_capture(conn, original=original, identifier=identifier),
        lambda: _resolve_geometry(conn, original=original, identifier=identifier),
    )
    for resolver in resolvers:
        result = resolver()
        if result is not None:
            if path_hint and not result.get("path"):
                result["path"] = path_hint
            return result

    return _base(
        reference=original,
        canonical_reference=receipt or identifier,
        kind="unknown",
        identifier=identifier,
        status="missing",
        label=_humanize_path(path_hint) or identifier,
        path=path_hint,
        metadata={
            "reason": "source_not_found_or_retained",
            "receipt_preserved": receipt is not None,
        },
    )
