"""Reverse-loop G1 (spec 2026-06-26 §3.1.3) — the ONLY content-bearing reverse
channel: ingest an app-distilled, desensitized **task-outcome** summary into
durable memory as a ``task-outcome-*.md`` entry.

When a proactive ``.context`` task finishes, the app distills what it produced
into a short, red-line-filtered ``summary`` and POSTs it here. We:

1. **idempotent by task_id** — a re-send (the app fires fire-and-forget) is a
   no-op (``task_outcome_ingests`` dedup table);
2. **daemon-side PII backstop** (``privacy.scrub``) — independent of the app's
   own filter; **宁缺毋滥**: ANY secret/PII hit DROPS the whole record rather
   than storing a partially-masked one;
3. write a ``task-outcome-<date>.md`` L-knowledge entry via the **legacy markdown
   write path** (``entries.create_file`` + ``append_entry``) — that prefix is
   **evo_nodes-exempt** (Q2, like ``event-*``), so it lands in markdown + the FTS
   retrieval projection (searchable, vector-enqueued when hybrid is on) but never
   the entity chain.

Forward contract intact: this only ADDS a memory entry; the daemon stays the
memory SSOT. Caller (the route) is responsible for the ``memory_ingest_enabled``
kill-switch and for staying best-effort/detached.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from ..logger import get
from ..privacy import scrub
from ..store import entries as entries_mod
from ..store import files as files_mod

logger = get("persome.memory.task_outcome")

_DEDUP_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_outcome_ingests (
    task_id TEXT PRIMARY KEY,       -- app task UUID — the idempotency key
    intent_id INTEGER,              -- the intent this execution served (NULL if none)
    entry_id TEXT,                  -- the written entry id (NULL when dropped by the PII gate)
    status TEXT NOT NULL,           -- ingested | dropped_pii
    ts TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_DEDUP_SCHEMA)


@dataclass
class IngestResult:
    status: str  # ingested | duplicate | dropped_pii | empty
    entry_id: str | None = None
    dropped_categories: list[str] = field(default_factory=list)


def _render_body(
    *, title: str, summary: str, kind: str, intent_id: int | None, artifact_types: list[str]
) -> str:
    """Render the durable card. Content = the app-distilled title + summary; the
    tail is content-light structured metadata. Artifact URLs are deliberately NOT
    stored (ephemeral + leak-prone) — only their TYPES (the durable signal)."""
    lines = [f"# {title}" if title else "# 执行产物"]
    if summary:
        lines += ["", summary]
    meta = [f"kind={kind or 'task-outcome'}"]
    if intent_id is not None:
        meta.append(f"intent={intent_id}")
    if artifact_types:
        meta.append("produced=" + ",".join(sorted(set(artifact_types))))
    lines += ["", "<!-- " + " ".join(meta) + " -->"]
    return "\n".join(lines)


def ingest_task_outcome(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    kind: str,
    title: str,
    summary: str,
    intent_id: int | None = None,
    artifact_types: list[str] | None = None,
    ts: str | None = None,
) -> IngestResult:
    """Ingest one task-outcome. See module docstring. Commits on its own connection."""
    ensure_schema(conn)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    task_id = (task_id or "").strip()
    if not task_id:
        return IngestResult("empty")

    # 1. idempotent by task_id (covers a resend of an already-ingested OR already-dropped one)
    if conn.execute("SELECT 1 FROM task_outcome_ingests WHERE task_id = ?", (task_id,)).fetchone():
        return IngestResult("duplicate")

    title = (title or "").strip()
    summary = (summary or "").strip()
    if not title and not summary:
        return IngestResult("empty")

    # 2. daemon-side PII backstop — 宁缺毋滥: any hit drops the whole record.
    res = scrub.scan(f"{title}\n{summary}")
    if not res.clean:
        logger.warning(
            "task-outcome ingest DROPPED by PII gate: task=%s categories=%s", task_id, res.hits
        )
        # Record the task_id so a resend doesn't re-scan/re-log forever (idempotent drop).
        conn.execute(
            "INSERT OR IGNORE INTO task_outcome_ingests "
            "(task_id, intent_id, entry_id, status, ts, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, intent_id, None, "dropped_pii", ts or now, now),
        )
        conn.commit()
        return IngestResult("dropped_pii", dropped_categories=res.hits)

    # 3. write via the LEGACY markdown path (task-outcome-* is evo_nodes-exempt).
    day = (ts or now)[:10]  # YYYY-MM-DD
    name = f"task-outcome-{day}.md"
    if not files_mod.memory_path(name).exists():
        entries_mod.create_file(
            conn, name=name, description="执行产物蒸馏（反向闭环 G1）", tags=["task-outcome"]
        )
    body = _render_body(
        title=title, summary=summary, kind=kind, intent_id=intent_id,
        artifact_types=artifact_types or [],
    )
    entry_id = entries_mod.append_entry(
        conn,
        name=name,
        content=body,
        tags=["task-outcome", kind or "task-outcome", f"task:{task_id}"],
        occurred_at=ts,
    )
    conn.execute(
        "INSERT INTO task_outcome_ingests "
        "(task_id, intent_id, entry_id, status, ts, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, intent_id, entry_id, "ingested", ts or now, now),
    )
    conn.commit()
    logger.info("task-outcome ingested: task=%s entry=%s kind=%s", task_id, entry_id, kind)
    return IngestResult("ingested", entry_id=entry_id)
