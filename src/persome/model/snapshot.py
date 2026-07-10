"""Deterministic, privacy-safe projection of the Point/Line/Face/Volume/Root model."""

from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import paths
from ..privacy.scrub import scan
from .manifest import create_build_manifest

SCHEMA_VERSION = 1
_REQUIRED_KEYS = {
    "schema_version",
    "generated_at",
    "build",
    "points",
    "lines",
    "faces",
    "volumes",
    "root",
    "receipts",
    "stats",
}


class ModelContractError(RuntimeError):
    """The stored model cannot be represented without violating the public contract."""


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _json_list(value: Any) -> list[Any]:
    if not value:
        return []
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _select(name: str, columns: set[str], fallback: str = "NULL") -> str:
    return name if name in columns else f"{fallback} AS {name}"


class _Redactor:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.counts: Counter[str] = Counter()

    def text(self, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value)
        if not self.enabled:
            return text
        result = scan(text)
        self.counts.update(result.hits)
        return result.redacted

    def texts(self, values: list[Any]) -> list[str]:
        return [cleaned for value in values if (cleaned := self.text(value)) is not None]


def _point_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if not _table_exists(conn, "evo_nodes"):
        return []
    columns = _columns(conn, "evo_nodes")
    selected = [
        _select("node_id", columns, "''"),
        _select("content", columns, "''"),
        _select("layer", columns, "''"),
        _select("supersedes", columns, "'[]'"),
        _select("superseded_by", columns, "'[]'"),
        _select("is_latest", columns, "1"),
        _select("status", columns, "'active'"),
        _select("file_name", columns, "''"),
        _select("tags", columns, "''"),
        _select("confidence", columns),
        _select("conflicted", columns, "0"),
        _select("occurred_at", columns),
        _select("valid_from", columns),
        _select("valid_until", columns),
        _select("gmt_created", columns),
    ]
    return list(
        conn.execute(
            f"SELECT {', '.join(selected)} FROM evo_nodes "
            "WHERE status != 'archived' ORDER BY node_id"
        ).fetchall()
    )


def _relation_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if not _table_exists(conn, "relation_edges"):
        return []
    columns = _columns(conn, "relation_edges")
    selected = [
        _select(name, columns)
        for name in (
            "edge_id",
            "src_identity",
            "dst_identity",
            "predicate",
            "label",
            "valid_from",
            "valid_to",
            "provenance",
            "confidence",
            "quote",
            "status",
            "created_at",
            "observations",
            "src_kind",
            "dst_kind",
            "polarity",
            "source_kind",
            "source_id",
            "source_receipt",
        )
    ]
    return list(
        conn.execute(
            f"SELECT {', '.join(selected)} FROM relation_edges "
            "WHERE valid_to IS NULL AND status = 'active' ORDER BY edge_id"
        ).fetchall()
    )


def _schema_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if not _table_exists(conn, "schema_faces"):
        return []
    columns = _columns(conn, "schema_faces")
    selected = [
        _select(name, columns, "'[]'" if name in {"members", "footprints", "anchors"} else "NULL")
        for name in (
            "face_id",
            "level",
            "parent_face",
            "signature",
            "members",
            "footprints",
            "provenance",
            "observations",
            "confidence",
            "status",
            "valid_from",
            "valid_to",
            "created_at",
            "anchors",
        )
    ]
    return list(
        conn.execute(
            f"SELECT {', '.join(selected)} FROM schema_faces "
            "WHERE valid_to IS NULL AND status = 'active' ORDER BY level, face_id"
        ).fetchall()
    )


def _schema_item(
    row: sqlite3.Row, redactor: _Redactor, point_receipts: dict[str, str]
) -> dict[str, Any]:
    members = [str(member) for member in _json_list(row["members"])]
    member_receipts = [point_receipts[member] for member in members if member in point_receipts]
    return {
        "id": row["face_id"],
        "level": int(row["level"]),
        "parent_id": row["parent_face"],
        "signature": redactor.text(row["signature"]),
        "members": members,
        "member_receipts": member_receipts,
        "source_receipts": member_receipts,
        "anchors": redactor.texts(_json_list(row["anchors"])),
        "provenance": row["provenance"],
        "observations": int(row["observations"] or 0),
        "confidence": float(row["confidence"] or 0.0),
        "status": row["status"],
        "valid_from": row["valid_from"],
        "created_at": row["created_at"],
    }


def _schema_member_aliases(
    conn: sqlite3.Connection, schema_items: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Map internal ``schema-*.md`` member keys back to their level-1 Face."""
    by_signature = {
        str(item["signature"] or "").strip().casefold(): item
        for item in schema_items
        if item["level"] == 1 and str(item["signature"] or "").strip()
    }
    if not by_signature or not _table_exists(conn, "entries"):
        return {}
    aliases: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT path, content FROM entries WHERE prefix = 'schema' AND superseded = 0"
    ).fetchall():
        central = ""
        for line in str(row["content"] or "").splitlines():
            if line.strip().casefold().startswith("central:"):
                central = line.split(":", 1)[1].strip().casefold()
                break
        if central and central in by_signature:
            aliases[str(row["path"])] = by_signature[central]
    return aliases


def build_snapshot(
    conn: sqlite3.Connection,
    *,
    redact: bool = True,
    build_metadata: dict[str, Any] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build a stable public projection from the current SQLite model state.

    This function is read-only. It does not run capture or model-building jobs; callers can
    invoke it after a build, or use it to inspect an already populated data root.
    """
    conn.row_factory = sqlite3.Row
    timestamp = generated_at or datetime.now(UTC).isoformat()
    redactor = _Redactor(redact)

    points: list[dict[str, Any]] = []
    receipts: dict[str, dict[str, Any]] = {}
    point_receipts: dict[str, str] = {}
    point_rows = _point_rows(conn)
    from ..store.schema_faces import member_key

    for row in point_rows:
        node_id = str(row["node_id"])
        file_name = redactor.text(row["file_name"]) or ""
        receipt = f"⟨{node_id}:{file_name}⟩"
        point_receipts[node_id] = receipt
        point_receipts.setdefault(member_key(str(row["content"] or "")), receipt)
        receipts[receipt] = {
            "receipt": receipt,
            "source_kind": "point",
            "source_id": node_id,
            "path": file_name,
        }
        points.append(
            {
                "id": node_id,
                "content": redactor.text(row["content"]),
                "layer": row["layer"],
                "supersedes": [str(value) for value in _json_list(row["supersedes"])],
                "superseded_by": [str(value) for value in _json_list(row["superseded_by"])],
                "is_latest": bool(row["is_latest"]),
                "status": row["status"],
                "file_name": file_name,
                "tags": redactor.text(row["tags"]) or "",
                "confidence": row["confidence"],
                "conflicted": bool(row["conflicted"]),
                "occurred_at": row["occurred_at"],
                "valid_from": row["valid_from"],
                "valid_until": row["valid_until"],
                "created_at": row["gmt_created"],
                "receipt": receipt,
            }
        )

    lines: list[dict[str, Any]] = []
    for point in points:
        for old_id in point["supersedes"]:
            lines.append(
                {
                    "id": f"evolution:{old_id}:{point['id']}",
                    "kind": "evolution",
                    "source": old_id,
                    "target": point["id"],
                    "predicate": "supersedes",
                    "receipt": point["receipt"],
                }
            )

    relation_count = 0
    for row in _relation_rows(conn):
        relation_count += 1
        source_receipt = redactor.text(row["source_receipt"])
        source_evidence = None
        if row["source_kind"] and row["source_id"] and source_receipt:
            source_evidence = {
                "kind": row["source_kind"],
                "id": row["source_id"],
                "receipt": source_receipt,
            }
            receipts[source_receipt] = {
                "receipt": source_receipt,
                "source_kind": row["source_kind"],
                "source_id": row["source_id"],
            }
        lines.append(
            {
                "id": row["edge_id"],
                "kind": "relation",
                "source": redactor.text(row["src_identity"]),
                "target": redactor.text(row["dst_identity"]),
                "source_entity_kind": row["src_kind"],
                "target_entity_kind": row["dst_kind"],
                "predicate": row["predicate"],
                "label": redactor.text(row["label"]),
                "quote": redactor.text(row["quote"]),
                "provenance": row["provenance"],
                "confidence": float(row["confidence"] or 0.0),
                "observations": int(row["observations"] or 0),
                "polarity": row["polarity"] or "0",
                "valid_from": row["valid_from"],
                "created_at": row["created_at"],
                "source_evidence": source_evidence,
            }
        )
    lines.sort(key=lambda line: (line["kind"], line["id"]))

    schema_items = [_schema_item(row, redactor, point_receipts) for row in _schema_rows(conn)]
    schema_by_id = {item["id"]: item for item in schema_items}
    schema_by_id.update(_schema_member_aliases(conn, schema_items))
    for item in schema_items:
        inherited = set(item["source_receipts"])
        for member_id in item["members"]:
            member = schema_by_id.get(member_id)
            if member is not None:
                inherited.update(member["source_receipts"])
        item["source_receipts"] = sorted(inherited)
    faces = [item for item in schema_items if item["level"] == 1]
    volumes = [item for item in schema_items if item["level"] == 2]
    roots = [item for item in schema_items if item["level"] == 3]
    if len(roots) > 1:
        raise ModelContractError(f"expected at most one live Root, found {len(roots)}")

    build = create_build_manifest(started_at=timestamp, completed_at=timestamp)
    build.update(build_metadata or {})
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": timestamp,
        "build": build,
        "points": points,
        "lines": lines,
        "faces": faces,
        "volumes": volumes,
        "root": roots[0] if roots else None,
        "receipts": [receipts[key] for key in sorted(receipts)],
        "stats": {
            "points": len(points),
            "active_points": sum(
                1 for point in points if point["is_latest"] and point["status"] == "active"
            ),
            "evolution_lines": len(lines) - relation_count,
            "relation_lines": relation_count,
            "faces": len(faces),
            "volumes": len(volumes),
            "roots": len(roots),
            "receipts": len(receipts),
            "redactions": dict(sorted(redactor.counts.items())),
        },
    }
    validate_snapshot(snapshot)
    return snapshot


def validate_snapshot(snapshot: dict[str, Any]) -> None:
    """Validate the stable top-level contract and the singleton Root invariant."""
    missing = _REQUIRED_KEYS - set(snapshot)
    if missing:
        raise ModelContractError(f"model snapshot missing keys: {sorted(missing)}")
    if snapshot["schema_version"] != SCHEMA_VERSION:
        raise ModelContractError(
            f"unsupported model snapshot schema_version={snapshot['schema_version']!r}"
        )
    for key in ("points", "lines", "faces", "volumes", "receipts"):
        if not isinstance(snapshot[key], list):
            raise ModelContractError(f"model snapshot field {key!r} must be a list")
    if snapshot["root"] is not None and not isinstance(snapshot["root"], dict):
        raise ModelContractError("model snapshot field 'root' must be an object or null")
    if int(snapshot["stats"].get("roots", 0)) not in (0, 1):
        raise ModelContractError("model snapshot must contain zero or one live Root")


def model_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return a compact readiness/status view backed by the same model contract."""
    snapshot = build_snapshot(conn)
    issues: list[str] = []
    if not snapshot["points"]:
        issues.append("no_points")
    if not snapshot["lines"]:
        issues.append("no_lines")
    if not snapshot["faces"]:
        issues.append("no_faces")
    if not snapshot["volumes"]:
        issues.append("no_volumes")
    if snapshot["root"] is None:
        issues.append("no_root")
    return {
        "schema_version": SCHEMA_VERSION,
        "ready": not issues,
        "issues": issues,
        "generated_at": snapshot["generated_at"],
        "stats": snapshot["stats"],
        "root_id": snapshot["root"]["id"] if snapshot["root"] else None,
    }


def export_snapshot(
    conn: sqlite3.Connection,
    *,
    out_path: Path | None = None,
    redact: bool = True,
    build_metadata: dict[str, Any] | None = None,
    generated_at: str | None = None,
) -> Path:
    """Atomically write a model snapshot. Exports are redacted and mode 0600 by default."""
    snapshot = build_snapshot(
        conn,
        redact=redact,
        build_metadata=build_metadata,
        generated_at=generated_at,
    )
    target = out_path
    if target is None:
        stamp = snapshot["generated_at"].replace(":", "-").replace("+", "_")
        target = paths.exports_dir() / f"model-snapshot-{stamp}.json"
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(target)
        os.chmod(target, 0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return target
