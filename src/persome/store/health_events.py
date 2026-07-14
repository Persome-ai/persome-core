"""Local persistence for normalized wearable and health observations."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS health_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    value_json TEXT NOT NULL,
    unit TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    timezone TEXT,
    device TEXT,
    device_id TEXT,
    metadata_json TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    UNIQUE(provider, external_id)
);
CREATE INDEX IF NOT EXISTS idx_health_events_metric_time
    ON health_events(metric, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_health_events_provider_time
    ON health_events(provider, started_at DESC);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def import_events(conn: sqlite3.Connection, events: list[dict[str, Any]]) -> dict[str, int]:
    """Atomically import a batch using provider IDs as revision identities.

    A byte-for-byte replay of the normalized event is a duplicate.  When a
    provider reuses an ID with changed content, the new payload is a correction
    of that observation rather than a second observation.
    """
    ensure_schema(conn)
    imported_at = datetime.now(UTC).isoformat(timespec="seconds")
    inserted = 0
    corrected = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for event in events:
            source = event["source"]
            values = (
                event["metric"],
                json.dumps(event["value"], ensure_ascii=False, separators=(",", ":")),
                event.get("unit") or "",
                _iso(event["started_at"]),
                _iso(event.get("ended_at")),
                event.get("timezone"),
                source.get("device"),
                source.get("device_id"),
                json.dumps(event.get("metadata") or {}, ensure_ascii=False, separators=(",", ":")),
            )
            existing = conn.execute(
                """
                SELECT metric, value_json, unit, started_at, ended_at, timezone,
                       device, device_id, metadata_json
                FROM health_events
                WHERE provider = ? AND external_id = ?
                """,
                (source["provider"], event["event_id"]),
            ).fetchone()
            if existing is not None and tuple(existing) == values:
                continue
            if existing is None:
                conn.execute(
                    """
                INSERT INTO health_events (
                    provider, external_id, metric, value_json, unit,
                    started_at, ended_at, timezone, device, device_id,
                    metadata_json, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        source["provider"],
                        event["event_id"],
                        *values,
                        imported_at,
                    ),
                )
                inserted += 1
            else:
                conn.execute(
                    """
                    UPDATE health_events
                    SET metric = ?, value_json = ?, unit = ?, started_at = ?,
                        ended_at = ?, timezone = ?, device = ?, device_id = ?,
                        metadata_json = ?, imported_at = ?
                    WHERE provider = ? AND external_id = ?
                    """,
                    (*values, imported_at, source["provider"], event["event_id"]),
                )
                corrected += 1
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    return {
        "received": len(events),
        "inserted": inserted,
        "corrected": corrected,
        "duplicates": len(events) - inserted - corrected,
    }


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def query_events(
    conn: sqlite3.Connection,
    *,
    metric: str | None = None,
    provider: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Read normalized observations newest-first with optional exact filters."""
    ensure_schema(conn)
    clauses: list[str] = []
    values: list[Any] = []
    for column, value in (("metric", metric), ("provider", provider)):
        if value:
            clauses.append(f"{column} = ?")
            values.append(value)
    if since:
        clauses.append("persome_epoch(started_at) >= persome_epoch(?)")
        values.append(since)
    if until:
        clauses.append("persome_epoch(started_at) < persome_epoch(?)")
        values.append(until)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    values.append(limit)
    rows = conn.execute(
        f"""
        SELECT provider, external_id, metric, value_json, unit, started_at,
               ended_at, timezone, device, metadata_json, imported_at
        FROM health_events
        {where}
        ORDER BY persome_epoch(started_at) DESC, id DESC
        LIMIT ?
        """,  # noqa: S608 - only fixed column fragments are interpolated
        values,
    ).fetchall()
    return [
        {
            "provider": row["provider"],
            "event_id": row["external_id"],
            "metric": row["metric"],
            "value": json.loads(row["value_json"]),
            "unit": row["unit"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "timezone": row["timezone"],
            "device": row["device"],
            "metadata": json.loads(row["metadata_json"]),
            "imported_at": row["imported_at"],
        }
        for row in rows
    ]
