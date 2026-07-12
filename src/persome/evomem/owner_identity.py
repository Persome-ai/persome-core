"""High-level owner identity resolver joining AI evidence to reserved ``self``."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from ..logger import get
from ..store import fts
from ..store import owner_aliases as alias_store

logger = get("persome.evomem.owner_identity")


def configured_aliases(cfg: Any) -> list[str]:
    values = getattr(getattr(cfg, "memory_delta", None), "owner_aliases", [])
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = alias_store.clean_alias(str(raw))
        if value is None or (key := alias_store.norm(value)) in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _stored_aliases(conn: sqlite3.Connection | None, statuses: set[str]) -> list[str]:
    if conn is not None:
        return alias_store.list_aliases(conn, statuses=statuses)
    try:
        with fts.cursor() as owned:
            return alias_store.list_aliases(owned, statuses=statuses)
    except Exception:  # noqa: BLE001 - owner evidence is fail-safe
        logger.debug("owner identity store read failed", exc_info=True)
        return []


def active_aliases(cfg: Any, *, conn: sqlite3.Connection | None = None) -> list[str]:
    values = [
        *configured_aliases(cfg),
        *_stored_aliases(conn, {alias_store.STATUS_ACTIVE}),
    ]
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = alias_store.norm(value)
        if key and key not in seen:
            seen.add(key)
            out.append(value)
    return out


def reserved_aliases(cfg: Any, *, conn: sqlite3.Connection | None = None) -> list[str]:
    values = [
        *active_aliases(cfg, conn=conn),
        *_stored_aliases(conn, {alias_store.STATUS_PENDING}),
    ]
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = alias_store.norm(value)
        if key and key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _matching_person_files(conn: sqlite3.Connection, alias: str) -> set[str]:
    key = alias_store.norm(alias)
    files: set[str] = set()
    try:
        rows = conn.execute(
            "SELECT file_name, content, schema_summary FROM evo_nodes"
            " WHERE is_latest=1 AND status='active' AND file_name LIKE 'person-%'"
        ).fetchall()
    except sqlite3.Error:
        return files
    for row in rows:
        file_name = str(row[0] or "")
        names = [str(row[1] or "")]
        try:
            meta = json.loads(row[2] or "{}")
            if isinstance(meta, dict):
                names.append(str(meta.get("canonical") or ""))
                names.extend(str(a) for a in (meta.get("aliases") or []))
        except (TypeError, ValueError):
            pass
        if any(alias_store.norm(name) == key for name in names):
            files.add(file_name)
    return files


def _retire_person_projection(conn: sqlite3.Connection, alias: str) -> None:
    """Retire an already-minted owner Point without deleting its audit history."""
    now = datetime.now(UTC).isoformat()
    files = _matching_person_files(conn, alias)
    if files:
        derived_files: set[str] = set()
        for file_name in files:
            stem = file_name.removesuffix(".md")
            derived_files.add(f"schema-{stem}.md")
            try:
                rows = conn.execute(
                    "SELECT DISTINCT file_name FROM evo_nodes"
                    " WHERE is_latest=1 AND status='active'"
                    " AND (file_name LIKE ? OR file_name LIKE ?)",
                    (f"schema-xdomain-{stem}__%", f"schema-xdomain-%__{stem}.md"),
                ).fetchall()
                derived_files.update(str(row[0]) for row in rows if row[0])
            except sqlite3.Error:
                pass
        files.update(derived_files)
        placeholders = ",".join("?" for _ in files)
        conn.execute(
            f"UPDATE evo_nodes SET status='shadow', is_latest=0,"
            f" valid_until=COALESCE(valid_until, ?) WHERE file_name IN ({placeholders})"
            " AND is_latest=1 AND status='active'",
            (now, *sorted(files)),
        )

    key = alias_store.norm(alias)
    try:
        rows = conn.execute(
            "SELECT edge_id, src_identity, dst_identity FROM relation_edges WHERE valid_to IS NULL"
        ).fetchall()
        edge_ids = [
            str(row[0])
            for row in rows
            if alias_store.norm(str(row[1])) == key or alias_store.norm(str(row[2])) == key
        ]
        if edge_ids:
            placeholders = ",".join("?" for _ in edge_ids)
            conn.execute(
                f"UPDATE relation_edges SET valid_to=?, status='archived'"
                f" WHERE edge_id IN ({placeholders}) AND valid_to IS NULL",
                (now, *edge_ids),
            )
    except sqlite3.Error:
        pass

    try:
        rows = conn.execute(
            "SELECT face_id, anchors FROM schema_faces WHERE valid_to IS NULL"
        ).fetchall()
        face_ids: list[str] = []
        for row in rows:
            try:
                anchors = json.loads(row[1] or "[]")
            except (TypeError, ValueError):
                anchors = []
            if any(alias_store.norm(str(anchor)) == key for anchor in anchors):
                face_ids.append(str(row[0]))
        if face_ids:
            placeholders = ",".join("?" for _ in face_ids)
            conn.execute(
                f"UPDATE schema_faces SET valid_to=?, status='archived'"
                f" WHERE face_id IN ({placeholders}) AND valid_to IS NULL",
                (now, *face_ids),
            )
    except sqlite3.Error:
        pass

    if files:
        try:
            from ..session import store as session_store

            session_store.increment_system_state(conn, "model_structure_dirty")
        except Exception:  # noqa: BLE001 - projection retirement remains valid
            logger.debug("could not mark model dirty after owner promotion", exc_info=True)


def record_candidate(
    conn: sqlite3.Connection,
    *,
    alias: str,
    session_id: str,
    source_kind: str,
    quote: str,
    confidence: float,
) -> alias_store.OwnerAliasState | None:
    state = alias_store.record_evidence(
        conn,
        alias=alias,
        session_id=session_id,
        source_kind=source_kind,
        quote=quote,
        confidence=confidence,
    )
    if state is not None and state.activated_now:
        _retire_person_projection(conn, state.alias)
    return state


def accept_alias(
    conn: sqlite3.Connection,
    alias: str,
    *,
    source_id: str,
    quote: str,
    decision_source: str = "user",
) -> alias_store.OwnerAliasState | None:
    state = record_candidate(
        conn,
        alias=alias,
        session_id=source_id,
        source_kind=alias_store.SOURCE_USER_CORRECTION,
        quote=quote or alias,
        confidence=1.0,
    )
    if state is not None:
        conn.execute(
            "UPDATE owner_aliases SET decision_source=? WHERE alias_key=?",
            (decision_source, state.alias_key),
        )
    return state


def reject_alias(
    conn: sqlite3.Connection, alias: str, *, decision_source: str = "user"
) -> alias_store.OwnerAliasState | None:
    return alias_store.reject(conn, alias, source=decision_source)
