"""Extract chat conversation content from raw captures with scroll-gap detection."""

from __future__ import annotations

import sqlite3
from datetime import datetime


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
        "SELECT timestamp, visible_text FROM captures "
        " WHERE app_name = ? AND timestamp >= ? AND timestamp <= ? "
        " ORDER BY timestamp ASC",
        (app_name, start_ts, end_ts),
    ).fetchall()

    if not rows:
        return "", 0, 0

    # Deduplicate consecutive identical visible_text snapshots.
    deduped: list[tuple[str, str]] = []
    for ts, text in rows:
        text = (text or "").strip()
        if not text:
            continue
        if deduped and deduped[-1][1] == text:
            continue
        deduped.append((ts, text))

    if not deduped:
        return "", 0, 0

    # Build output segments with gap markers between non-overlapping consecutive pairs.
    segments: list[str] = []
    gap_count = 0

    for i, (ts, text) in enumerate(deduped):
        try:
            label = datetime.fromisoformat(ts).strftime("%H:%M:%S")
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
