"""Read stable schema inferences from the durable personal model."""

from __future__ import annotations

import sqlite3

from ..writer import schema_miner_stage as stage

_MAX_INFERENCES = 8
_STABLE_TAG = "stable"


def _confidence_of(tag_field: str) -> float:
    """Parse ``confidence:<float>`` from a space-separated tag field."""
    for token in tag_field.split():
        if token.startswith("confidence:"):
            try:
                return float(token.split(":", 1)[1])
            except ValueError:
                return 0.0
    return 0.0


def active_schema_inferences(conn: sqlite3.Connection) -> list[str]:
    """Return de-duplicated inference lines from the strongest stable schemas."""
    return [text for text, _source in active_schema_inferences_with_sources(conn)]


def active_schema_inferences_with_sources(
    conn: sqlite3.Connection,
) -> list[tuple[str, str]]:
    """Return stable inference lines paired with their schema memory file."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT path, tags, content FROM entries "
        "WHERE prefix = 'schema' AND superseded = 0 ORDER BY timestamp DESC"
    ).fetchall()
    stable = [row for row in rows if _STABLE_TAG in (row["tags"] or "").split()]
    stable.sort(key=lambda row: _confidence_of(row["tags"] or ""), reverse=True)

    output: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in stable:
        for line in stage.parse_expected_inferences(row["content"] or ""):
            if not line or line in seen:
                continue
            seen.add(line)
            output.append((line, row["path"]))
            if len(output) >= _MAX_INFERENCES:
                return output
    return output
