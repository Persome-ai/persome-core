"""Agent-Native Persome Phase 3 — durable memory write-back from a dispatched agent.

The load-bearing loop (spec docs/superpowers/specs/2026-06-25-agent-native-persome-design.md §6):
an agent's findings become durable Persome memory the NEXT agent / the recognizer / the supervisor
can reuse. Writes funnel through the canonical memory writer (``store.entries.append_entry``), so
they pass the same ``ensure_writes_allowed`` integrity gate + evomem write-inversion as every other
writer — NO bypass.

Every agent-written entry is force-tagged ``source:agent-run`` so it stays distinguishable from
recognizer- and user-authored memory forever (queryable, auditable, reversible). An optional
``run_id`` (the agent passes its ``$PERSOME_TASK_ID``) adds ``run:<id>`` for per-run attribution —
the daemon is a shared process and can't infer which dispatched run is calling.
"""

from __future__ import annotations

import contextlib
import sqlite3
from typing import Any

from ..store import entries as entries_mod
from ..store import files as files_mod

# A single dedicated file for agent-written findings (spec §12 "start simple"). Uses the
# ``topic-`` prefix (a valid memory prefix), so it round-trips the normal markdown-SSOT path.
AGENT_FINDINGS_FILE = "topic-agent-findings.md"
_AGENT_FINDINGS_DESC = (
    "Durable findings written back by Persome agents after completing runs (Agent-Native Persome)."
)

#: Always injected — the provenance marker that separates agent-authored memory from the rest.
PROVENANCE_TAG = "source:agent-run"


def _ensure_findings_file(conn: sqlite3.Connection) -> None:
    """Create the agent-findings file on first write (``append_entry`` requires it to exist)."""
    if files_mod.memory_path(AGENT_FINDINGS_FILE).exists():
        return
    # A concurrent remember() may win the create race — that's fine, it exists now.
    with contextlib.suppress(FileExistsError):
        entries_mod.create_file(
            conn,
            name=AGENT_FINDINGS_FILE,
            description=_AGENT_FINDINGS_DESC,
            tags=["agent"],
        )


def remember(
    conn: sqlite3.Connection,
    *,
    content: str,
    tags: list[str] | None = None,
    run_id: str = "",
) -> dict[str, Any]:
    """Append an agent finding to durable memory; return its entry id + the tags written.

    Force-injects ``source:agent-run`` (and ``run:<run_id>`` when provided), then any caller tags.
    Raises ``ValueError`` on empty content.
    """
    content = (content or "").strip()
    if not content:
        raise ValueError("content is required")
    _ensure_findings_file(conn)

    prov = [PROVENANCE_TAG]
    rid = run_id.strip()
    if rid:
        prov.append(f"run:{rid}")
    all_tags = prov + [t.strip() for t in (tags or []) if t and t.strip()]

    entry_id = entries_mod.append_entry(
        conn, name=AGENT_FINDINGS_FILE, content=content, tags=all_tags
    )
    return {"entry_id": entry_id, "file": AGENT_FINDINGS_FILE, "tags": all_tags}
