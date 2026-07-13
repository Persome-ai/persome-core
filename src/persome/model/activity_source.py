"""Versioned, auditable Activity inputs independent of the intent product layer."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from ..capture.timestamps import parse_capture_timestamp
from ..timeline import store as timeline_store

ACTIVITY_PREFIX = "event:"
SOURCE_KINDS = frozenset({"entry", "session", "intent"})
_DONE_INTENT_STATUSES = ("consumed", "completed")

ParticipantResolver = Callable[[list[str], str], list[str]]


def _dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value and value.strip()))


def _default_participant_resolver(names: list[str], _summary: str) -> list[str]:
    return _dedupe(names)


def _parse_time(value: object) -> datetime | None:
    return parse_capture_timestamp(value) if isinstance(value, str) and value else None


def _time_sort_key(value: object) -> datetime:
    parsed = _parse_time(value)
    return parsed.astimezone(UTC) if parsed is not None else datetime.min.replace(tzinfo=UTC)


def normalize_activity_identity(identity: str) -> str:
    """Map the old ``event:<intent-id>`` namespace into the canonical legacy namespace."""
    value = str(identity or "").strip()
    if not value.startswith(ACTIVITY_PREFIX):
        return value
    if any(value.startswith(f"event:{kind}:") for kind in SOURCE_KINDS):
        return value
    legacy_id = value.removeprefix(ACTIVITY_PREFIX)
    return f"event:intent:{legacy_id}" if legacy_id else value


def is_activity_identity(identity: str) -> bool:
    normalized = normalize_activity_identity(identity)
    return any(normalized.startswith(f"event:{kind}:") for kind in SOURCE_KINDS)


@dataclass(frozen=True)
class ActivityEvent:
    stable_id: str
    occurred_at: str | None
    summary: str
    participant_ids: list[str]
    source_kind: str
    source_id: str
    source_receipt: str

    def __post_init__(self) -> None:
        kind = self.source_kind.strip()
        source_id = self.source_id.strip()
        receipt = self.source_receipt.strip()
        expected = f"event:{kind}:{source_id}"
        if kind not in SOURCE_KINDS:
            raise ValueError(f"unsupported activity source_kind: {kind!r}")
        if not source_id or not receipt:
            raise ValueError("activity source_id and source_receipt must be non-empty")
        if self.stable_id != expected:
            raise ValueError(f"activity stable_id must be {expected!r}")


class ActivitySource:
    """Read past-tense activities from durable entries/sessions plus optional legacy intents."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        participant_resolver: ParticipantResolver | None = None,
        include_legacy_intents: bool = True,
        limit: int = 500,
    ) -> None:
        self._conn = conn
        self._resolve = participant_resolver or _default_participant_resolver
        self._include_legacy = include_legacy_intents
        self._limit = max(1, int(limit))

    def events(self) -> list[ActivityEvent]:
        """Return newest-first, stable-ID-deduplicated past activities."""
        previous_factory = self._conn.row_factory
        self._conn.row_factory = sqlite3.Row
        try:
            events = [*self._entry_events(), *self._session_events()]
            if self._include_legacy:
                events.extend(self._legacy_intent_events())
            unique = {event.stable_id: event for event in events}
            return sorted(
                unique.values(),
                key=lambda event: (_time_sort_key(event.occurred_at), event.stable_id),
                reverse=True,
            )[: self._limit]
        finally:
            self._conn.row_factory = previous_factory

    def event(self, identity: str) -> ActivityEvent | None:
        """Return one activity by stable identity without expanding the feed.

        Evidence drill-down is an exact lookup.  Rebuilding the bounded recent
        feed for it is both incorrect for retained activities older than
        ``limit`` and needlessly expensive because feed construction expands
        every candidate session into timeline blocks.
        """
        normalized = normalize_activity_identity(identity)
        parts = normalized.split(":", 2)
        if len(parts) != 3 or parts[0] != "event" or parts[1] not in SOURCE_KINDS:
            return None
        kind, source_id = parts[1], parts[2].strip()
        if not source_id:
            return None

        previous_factory = self._conn.row_factory
        self._conn.row_factory = sqlite3.Row
        try:
            if kind == "entry":
                try:
                    row = self._conn.execute(
                        "SELECT id, path, timestamp, content FROM entries "
                        "WHERE id = ? AND prefix = 'event' LIMIT 1",
                        (source_id,),
                    ).fetchone()
                except sqlite3.Error:
                    return None
                return self._entry_event(row) if row is not None else None

            if kind == "session":
                try:
                    row = self._conn.execute(
                        "SELECT id, start_time, end_time FROM sessions "
                        "WHERE id = ? AND status != 'active' AND end_time IS NOT NULL LIMIT 1",
                        (source_id,),
                    ).fetchone()
                except sqlite3.Error:
                    return None
                return self._session_event(row) if row is not None else None

            if not self._include_legacy:
                return None
            placeholders = ",".join("?" * len(_DONE_INTENT_STATUSES))
            try:
                row = self._conn.execute(
                    f"SELECT id, ts, kind, rationale, payload FROM intents "
                    f"WHERE id = ? AND (status IN ({placeholders}) "
                    "OR (status = 'resolved' AND resolution_outcome = 'done')) LIMIT 1",
                    (source_id, *_DONE_INTENT_STATUSES),
                ).fetchone()
            except sqlite3.Error:
                return None
            return self._legacy_intent_event(row) if row is not None else None
        finally:
            self._conn.row_factory = previous_factory

    def _entry_event(self, row: sqlite3.Row) -> ActivityEvent | None:
        entry_id, path, timestamp, content = map(lambda value: value or "", row[:4])
        entry_id = str(entry_id).strip()
        summary = str(content).strip()
        if not entry_id or not summary:
            return None
        return ActivityEvent(
            stable_id=f"event:entry:{entry_id}",
            occurred_at=str(timestamp) or None,
            summary=summary,
            participant_ids=self._resolve([], summary),
            source_kind="entry",
            source_id=entry_id,
            source_receipt=f"⟨{entry_id}:{path}⟩",
        )

    def _session_event(self, row: sqlite3.Row) -> ActivityEvent | None:
        session_id = str(row[0] or "").strip()
        start = _parse_time(row[1])
        end = _parse_time(row[2])
        if not session_id or start is None or end is None:
            return None
        try:
            blocks = timeline_store.query_range(self._conn, start, end, limit=200)
        except sqlite3.Error:
            blocks = []
        summary = "\n".join(
            entry.strip() for block in blocks for entry in block.entries if entry and entry.strip()
        )[:2000]
        if not summary:
            summary = f"Session ended at {end.isoformat()}"
        return ActivityEvent(
            stable_id=f"event:session:{session_id}",
            occurred_at=end.isoformat(),
            summary=summary,
            participant_ids=self._resolve([], summary),
            source_kind="session",
            source_id=session_id,
            source_receipt=f"⟨{session_id}:sessions⟩",
        )

    def _legacy_intent_event(self, row: sqlite3.Row) -> ActivityEvent | None:
        source_id = str(row[0] or "").strip()
        if not source_id:
            return None
        try:
            payload = json.loads(row[4] or "{}")
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        raw_people = payload.get("with") or payload.get("participants") or []
        names = [str(name) for name in raw_people] if isinstance(raw_people, list) else []
        summary = str(row[3] or "").strip() or f"Completed {row[2] or 'activity'}"
        return ActivityEvent(
            stable_id=f"event:intent:{source_id}",
            occurred_at=str(row[1]) if row[1] else None,
            summary=summary,
            participant_ids=self._resolve(names, summary),
            source_kind="intent",
            source_id=source_id,
            source_receipt=f"⟨{source_id}:intents⟩",
        )

    def _entry_events(self) -> list[ActivityEvent]:
        try:
            rows = self._conn.execute(
                "SELECT id, path, timestamp, content FROM entries "
                "WHERE prefix = 'event' AND superseded = 0 "
                "ORDER BY persome_epoch(timestamp) DESC LIMIT ?",
                (self._limit,),
            ).fetchall()
        except sqlite3.Error:
            return []
        return [event for row in rows if (event := self._entry_event(row)) is not None]

    def _session_events(self) -> list[ActivityEvent]:
        try:
            rows = self._conn.execute(
                "SELECT id, start_time, end_time FROM sessions "
                "WHERE status != 'active' AND end_time IS NOT NULL "
                "ORDER BY persome_epoch(end_time) DESC LIMIT ?",
                (self._limit,),
            ).fetchall()
        except sqlite3.Error:
            return []
        return [event for row in rows if (event := self._session_event(row)) is not None]

    def _legacy_intent_events(self) -> list[ActivityEvent]:
        placeholders = ",".join("?" * len(_DONE_INTENT_STATUSES))
        try:
            rows = self._conn.execute(
                f"SELECT id, ts, kind, rationale, payload FROM intents "
                f"WHERE status IN ({placeholders}) "
                "OR (status = 'resolved' AND resolution_outcome = 'done') "
                "ORDER BY persome_epoch(ts) DESC LIMIT ?",
                (*_DONE_INTENT_STATUSES, self._limit),
            ).fetchall()
        except sqlite3.Error:
            return []
        return [event for row in rows if (event := self._legacy_intent_event(row)) is not None]
