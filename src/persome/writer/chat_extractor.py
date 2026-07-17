"""Extract chat conversation content from raw captures with scroll-gap detection."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from .. import paths
from ..capture import s1_parser


def _safe_visible_text(capture_id: str, indexed_text: Any) -> str:
    """Return placeholder-safe text when the raw capture proves the repair.

    The capture index can outlive the S1 projection that produced it.  Use the
    raw AX tree to repair those legacy rows, but keep the indexed text as the
    fail-open value when the matching JSON is unavailable, malformed, or lacks
    structural placeholder evidence.  OCR backfills live only in SQLite, so an
    OCR-submitted capture with an empty safe AX projection sanitizes (rather
    than replaces) that DB-only text.
    """
    db_text = str(indexed_text or "")
    path = paths.capture_buffer_dir() / f"{capture_id}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return db_text
    if not isinstance(raw, dict):
        return db_text

    projection = s1_parser.sanitize_capture(raw)
    safe_ax_text = str(projection.get("visible_text") or "")
    if bool(raw.get("ocr_submitted")) and not safe_ax_text:
        # OCR is a flat pixel projection.  Only evidence that the placeholder
        # was actually visible can authorize removing an exact OCR field.
        if not s1_parser.ocr_placeholder_values(raw):
            return db_text
        return s1_parser.sanitize_ocr_text(raw, db_text)

    # Non-OCR S1 can be repaired from the local editable/placeholder pairing
    # even when a filled control hides its standard hint from OCR.  Require a
    # concrete change to an authored-text field before replacing the indexed
    # value: tree-only metadata changes are not enough to trust a recomputed
    # projection over SQLite.
    raw_focus = raw.get("focused_element")
    safe_focus = projection.get("focused_element")
    visible_repaired = safe_ax_text != str(raw.get("visible_text") or "")
    focus_repaired = (
        isinstance(raw_focus, dict)
        and isinstance(safe_focus, dict)
        and any(
            str(raw_focus.get(key) or "") != str(safe_focus.get(key) or "")
            for key in ("title", "value")
        )
    )
    if not visible_repaired and not focus_repaired:
        return db_text
    return safe_ax_text


def extract_chat_messages(
    conn: sqlite3.Connection,
    app_name: str,
    start_ts: str,
    end_ts: str,
    max_bytes: int = 12_000,
) -> tuple[str, int, int]:
    """Reconstruct conversation from captures for a chat app within a time window.

    Returns (formatted_text, snapshot_count, gap_count).

    Deduplicates consecutive identical snapshots, detects scroll gaps (no line
    overlap between consecutive snapshots), and truncates to max_bytes (keeping
    the most recent content).
    """
    rows = conn.execute(
        "SELECT id, timestamp, visible_text FROM captures "
        " WHERE app_name = ? "
        "   AND persome_epoch(timestamp) >= persome_epoch(?) "
        "   AND persome_epoch(timestamp) < persome_epoch(?) "
        " ORDER BY persome_epoch(timestamp) ASC",
        (app_name, start_ts, end_ts),
    ).fetchall()

    if not rows:
        return "", 0, 0

    # Deduplicate consecutive identical visible_text snapshots.
    deduped: list[tuple[str, str]] = []
    for capture_id, ts, indexed_text in rows:
        text = _safe_visible_text(str(capture_id), indexed_text).strip()
        if not text:
            continue
        if deduped and deduped[-1][1] == text:
            continue
        deduped.append((ts, text))

    if not deduped:
        return "", 0, 0

    try:
        display_tz = datetime.fromisoformat(start_ts).tzinfo
    except (TypeError, ValueError):
        display_tz = None

    # Build output segments with gap markers between non-overlapping consecutive pairs.
    segments: list[str] = []
    gap_count = 0

    for i, (ts, text) in enumerate(deduped):
        try:
            parsed = datetime.fromisoformat(ts)
            if parsed.tzinfo is not None:
                parsed = (
                    parsed.astimezone(display_tz) if display_tz is not None else parsed.astimezone()
                )
            label = parsed.strftime("%H:%M:%S")
        except (TypeError, ValueError):
            label = ts

        if i > 0:
            prev_lines = set(deduped[i - 1][1].splitlines())
            curr_lines = set(text.splitlines())
            # Remove empty lines from overlap check.
            prev_lines.discard("")
            curr_lines.discard("")
            if prev_lines and curr_lines and not prev_lines & curr_lines:
                segments.append("⚠️ [gap: fast scroll detected — content may be missing]")
                gap_count += 1

        segments.append(f"[{label}]\n{text}")

    full_text = "\n\n".join(segments)

    # Truncate to max_bytes, keeping most recent content.
    if len(full_text.encode()) > max_bytes:
        encoded = full_text.encode()
        trimmed = encoded[-max_bytes:].decode(errors="replace")
        # Find the first newline so we don't start mid-line.
        nl = trimmed.find("\n")
        if nl != -1:
            trimmed = trimmed[nl + 1 :]
        full_text = "…[truncated — showing most recent content]\n\n" + trimmed

    return full_text, len(deduped), gap_count
