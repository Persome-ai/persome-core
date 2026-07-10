"""Entity/person events derived from durable classifier memory, not product intents."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass

_ENTITY_PREFIXES = {"person-": "person", "org-": "org", "project-": "project"}
_DERIVED_PERSON_TAGS = ("person-entity", "person-event")


@dataclass(frozen=True)
class EntityEvent:
    stable_id: str
    entity_id: str
    display_name: str
    kind: str
    occurred_at: str | None
    summary: str
    confidence: float
    source_kind: str
    source_id: str
    source_receipt: str


def _entity_from_file(file_name: str) -> tuple[str, str] | None:
    stem = str(file_name or "").removesuffix(".md")
    for prefix, kind in _ENTITY_PREFIXES.items():
        if stem.startswith(prefix):
            entity_id = stem.removeprefix(prefix).strip()
            return (entity_id, kind) if entity_id else None
    return None


def _confidence(value: object) -> float:
    return {"high": 0.95, "medium": 0.75, "low": 0.55}.get(str(value or "").lower(), 0.8)


class EntitySource:
    """Read typed entity evidence from current evomem facts and durable event entries."""

    def __init__(self, conn: sqlite3.Connection, *, limit: int = 500) -> None:
        self._conn = conn
        self._limit = max(1, int(limit))

    def events(self) -> list[EntityEvent]:
        direct = self._direct_events()
        known = {(event.entity_id, event.kind): event.display_name for event in direct}
        mentions = self._event_mentions(known)
        unique = {event.stable_id: event for event in [*direct, *mentions]}
        return sorted(
            unique.values(),
            key=lambda event: (event.occurred_at or "", event.stable_id),
            reverse=True,
        )[: self._limit]

    def _direct_events(self) -> list[EntityEvent]:
        try:
            rows = self._conn.execute(
                "SELECT node_id, file_name, content, occurred_at, memory_at, confidence, tags "
                "FROM evo_nodes WHERE is_latest = 1 AND status = 'active' "
                "AND (file_name LIKE 'person-%' OR file_name LIKE 'org-%' "
                "OR file_name LIKE 'project-%') ORDER BY COALESCE(occurred_at, memory_at) DESC "
                "LIMIT ?",
                (self._limit,),
            ).fetchall()
        except sqlite3.Error:
            return []
        out: list[EntityEvent] = []
        for row in rows:
            tags = str(row[6] or "").split()
            if any(tag in tags for tag in _DERIVED_PERSON_TAGS):
                continue
            typed = _entity_from_file(str(row[1] or ""))
            if typed is None:
                continue
            entity_id, kind = typed
            node_id = str(row[0])
            summary = str(row[2] or "").strip()
            if not summary:
                continue
            out.append(
                EntityEvent(
                    stable_id=f"entity:point:{node_id}",
                    entity_id=entity_id,
                    display_name=entity_id,
                    kind=kind,
                    occurred_at=str(row[3] or row[4]) if (row[3] or row[4]) else None,
                    summary=summary,
                    confidence=_confidence(row[5]),
                    source_kind="point",
                    source_id=node_id,
                    source_receipt=f"⟨{node_id}:{row[1] or ''}⟩",
                )
            )
        return out

    def _event_mentions(self, known: dict[tuple[str, str], str]) -> list[EntityEvent]:
        if not known:
            return []
        try:
            rows = self._conn.execute(
                "SELECT id, path, timestamp, content FROM entries "
                "WHERE prefix = 'event' AND superseded = 0 ORDER BY timestamp DESC LIMIT ?",
                (self._limit,),
            ).fetchall()
        except sqlite3.Error:
            return []
        out: list[EntityEvent] = []
        for row in rows:
            entry_id = str(row[0] or "")
            content = str(row[3] or "").strip()
            folded = content.casefold()
            if not entry_id or not content:
                continue
            for (entity_id, kind), display_name in known.items():
                if display_name.casefold() not in folded and entity_id.casefold() not in folded:
                    continue
                out.append(
                    EntityEvent(
                        stable_id=f"entity:entry:{entry_id}:{kind}:{entity_id}",
                        entity_id=entity_id,
                        display_name=display_name,
                        kind=kind,
                        occurred_at=str(row[2]) if row[2] else None,
                        summary=content,
                        confidence=0.85,
                        source_kind="entry",
                        source_id=entry_id,
                        source_receipt=f"⟨{entry_id}:{row[1] or ''}⟩",
                    )
                )
        return out


class MemoryPersonNameSource:
    """``PersonNameSource`` adapter backed by durable person facts and event entries."""

    def __init__(
        self,
        *,
        conn_factory: Callable[[], object] | None = None,
        limit: int = 500,
    ) -> None:
        self._conn_factory = conn_factory
        self._limit = limit

    def events(self):  # type: ignore[no-untyped-def]
        from ..evomem.person_graph import PersonEvent, _parse_ts

        try:
            if self._conn_factory is not None:
                candidate = self._conn_factory()
                context = candidate if hasattr(candidate, "__enter__") else nullcontext(candidate)
            else:
                from ..store import fts

                context = fts.cursor()
            with context as conn:
                events = EntitySource(conn, limit=self._limit).events()
        except Exception:  # noqa: BLE001 — model enrichment is fail-safe
            return []
        return [
            PersonEvent(
                name=event.display_name,
                summary=event.summary,
                occurred_at=_parse_ts(event.occurred_at),
                confidence=event.confidence,
            )
            for event in events
            if event.kind == "person"
        ]
