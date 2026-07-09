"""Cross-file offline consolidation with provenance inspection.

The classifier writes durable facts continuously, one session at a time.
Across many sessions the same fact accumulates in slightly different
phrasings, contradictions go unresolved, and groups of related entries
beg to be lifted into one higher-level abstraction. The consolidator is
the offline pass that cleans that up.

It is **not** an inline stage of the daemon pipeline — it is triggered
manually (CLI / MCP) or by a low-frequency tick. The flow:

1. Take a small set of recently classified ``session_ids`` as the trigger.
2. Pull every non-superseded entry tagged with one of those sessions.
3. For each, BM25-retrieve nearby neighbours across *all* memory files.
4. Union + dedupe + cap → the "working region".
5. Run an LLM tool-call loop with read / search / **inspect_source** /
   supersede / append / commit. No ``create`` — we don't introduce files.
6. In dry-run mode, intercept supersede/append before they hit the DB
   and report the planned operations.

The ``inspect_source`` tool resolves an entry back to the raw timeline
blocks of its originating session — letting the LLM ground a borderline
rewrite on what actually happened rather than the classifier's earlier
compression.
"""

from __future__ import annotations

import functools
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from ..config import Config
from ..logger import get
from ..prompts import load as load_prompt
from ..store import entries as entries_mod
from ..store import fts
from . import llm as llm_mod
from . import tools as tools_mod

logger = get("persome.writer")


# Soft cap on inspect_source rendered text — ≈500 tokens at the 4-bytes-per-token
# heuristic used elsewhere in the writer.
_INSPECT_SOURCE_CHAR_LIMIT = 2000


@dataclass
class ConsolidateResult:
    """Outcome of one ``consolidate_region`` call.

    ``superseded_ids`` lists the original entries that the LLM marked for
    replacement (whether the writes hit disk or were intercepted by
    ``dry_run``). ``written_ids`` lists the *new* entry ids produced by
    successful supersede/append calls in normal mode, or the LLM-provided
    payloads (no real id) in dry-run mode.
    """

    triggered_by_sessions: list[str]
    committed: bool = False
    summary: str = ""
    superseded_ids: list[str] = field(default_factory=list)
    written_ids: list[str] = field(default_factory=list)
    iterations: int = 0
    dry_run: bool = False
    skipped_reason: str = ""
    region_size: int = 0
    planned_ops: list[dict[str, Any]] = field(default_factory=list)


# ─── inspect_source tool ─────────────────────────────────────────────────


def _entry_row(conn: sqlite3.Connection, entry_id: str) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT id, path, timestamp, tags, content, superseded FROM entries WHERE id=?",
        (entry_id,),
    ).fetchone()
    return cast("sqlite3.Row | None", row)


def _session_id_from_tags(tags: str) -> str:
    for tok in (tags or "").split():
        if tok.startswith("sid:"):
            return tok.split(":", 1)[1]
        if tok.startswith("#sid:"):
            return tok.split(":", 1)[1]
    return ""


def _session_window(conn: sqlite3.Connection, session_id: str) -> tuple[datetime, datetime] | None:
    if not session_id:
        return None
    row = conn.execute(
        "SELECT start_time, end_time FROM sessions WHERE id=?", (session_id,)
    ).fetchone()
    if row is None:
        return None
    try:
        start = datetime.fromisoformat(row["start_time"])
    except (TypeError, ValueError):
        return None
    end_raw = row["end_time"]
    try:
        end = datetime.fromisoformat(end_raw) if end_raw else datetime.now().astimezone()
    except (TypeError, ValueError):
        end = datetime.now().astimezone()
    return start, end


def _render_blocks(conn: sqlite3.Connection, start: datetime, end: datetime) -> str:
    rows = conn.execute(
        """
        SELECT start_time, end_time, entries, apps_used
          FROM timeline_blocks
         WHERE end_time > ? AND start_time < ?
         ORDER BY start_time ASC
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    if not rows:
        return ""
    out: list[str] = []
    for r in rows:
        try:
            s = datetime.fromisoformat(r["start_time"]).strftime("%H:%M")
            e = datetime.fromisoformat(r["end_time"]).strftime("%H:%M")
        except (TypeError, ValueError):
            s, e = r["start_time"], r["end_time"]
        block_entries = json.loads(r["entries"] or "[]")
        out.append(f"[{s}-{e}]")
        out.extend(f"  - {entry}" for entry in block_entries)
        if len("\n".join(out)) > _INSPECT_SOURCE_CHAR_LIMIT:
            break
    text = "\n".join(out)
    if len(text) > _INSPECT_SOURCE_CHAR_LIMIT:
        text = text[:_INSPECT_SOURCE_CHAR_LIMIT].rstrip() + "\n… (truncated)"
    return text


def tool_inspect_source(conn: sqlite3.Connection, *, entry_id: str) -> dict[str, Any]:
    """Resolve ``entry_id`` → its session's timeline blocks (read-only).

    Steps:
        1. Look up the entry's row to recover its ``sid:<session_id>`` tag.
        2. Resolve that session's ``[start_time, end_time)`` window.
        3. Return rendered timeline blocks covering that window.

    Returns ``{"text": "", "found": False, ...}`` if any step fails — the
    LLM should treat absence as "no source available", not an error.
    """
    if not entry_id:
        return {"text": "", "found": False, "reason": "entry_id required"}
    row = _entry_row(conn, entry_id)
    if row is None:
        return {"text": "", "found": False, "reason": f"entry {entry_id} not found"}
    session_id = _session_id_from_tags(row["tags"] or "")
    if not session_id:
        return {
            "text": "",
            "found": False,
            "reason": f"entry {entry_id} has no sid:* tag",
            "entry_path": row["path"],
            "entry_timestamp": row["timestamp"],
        }
    window = _session_window(conn, session_id)
    if window is None:
        return {
            "text": "",
            "found": False,
            "reason": f"session {session_id} not in sessions table",
            "session_id": session_id,
        }
    start, end = window
    text = _render_blocks(conn, start, end)
    return {
        "text": text,
        "found": bool(text),
        "session_id": session_id,
        "session_start": start.isoformat(),
        "session_end": end.isoformat(),
        "entry_path": row["path"],
        "entry_timestamp": row["timestamp"],
    }


_INSPECT_SOURCE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "inspect_source",
        "description": (
            "Resolve a memory entry back to the raw timeline blocks of "
            "its originating session. Use to ground an ambiguous rewrite "
            "on what actually happened, not on the classifier's earlier "
            "summary. Returns empty text if no timeline data is found."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "entry_id": {"type": "string"},
            },
            "required": ["entry_id"],
        },
    },
}


# Consolidator-specific tool surface. No ``create`` — the consolidator
# cleans up existing files but does not introduce new ones.
CONSOLIDATOR_SCHEMAS: list[dict[str, Any]] = [
    next(t for t in tools_mod.TOOL_SCHEMAS if t["function"]["name"] == "read_memory"),
    next(t for t in tools_mod.TOOL_SCHEMAS if t["function"]["name"] == "search_memory"),
    _INSPECT_SOURCE_SCHEMA,
    next(t for t in tools_mod.TOOL_SCHEMAS if t["function"]["name"] == "supersede"),
    next(t for t in tools_mod.TOOL_SCHEMAS if t["function"]["name"] == "append"),
    next(t for t in tools_mod.TOOL_SCHEMAS if t["function"]["name"] == "commit"),
]

CONSOLIDATOR_TOOL_NAMES = {t["function"]["name"] for t in CONSOLIDATOR_SCHEMAS}


# ─── working region assembly ─────────────────────────────────────────────


@dataclass
class _RegionEntry:
    id: str
    path: str
    timestamp: str
    tags: str
    content: str


def _session_entries(conn: sqlite3.Connection, session_ids: list[str]) -> list[_RegionEntry]:
    """Every non-superseded entry tagged with one of ``session_ids``.

    Uses a substring match on the ``tags`` column rather than FTS — the
    tags column is short, indexing it by FTS would split the ``sid:``
    prefix from the value, and the candidate sessions list is small.
    """
    out: list[_RegionEntry] = []
    seen: set[str] = set()
    for sid in session_ids:
        rows = conn.execute(
            """
            SELECT id, path, timestamp, tags, content
              FROM entries
             WHERE superseded = 0
               AND (tags LIKE ? OR tags LIKE ? OR tags LIKE ? OR tags = ?)
            """,
            (
                f"sid:{sid} %",
                f"% sid:{sid} %",
                f"% sid:{sid}",
                f"sid:{sid}",
            ),
        ).fetchall()
        for r in rows:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            out.append(
                _RegionEntry(
                    id=r["id"],
                    path=r["path"],
                    timestamp=r["timestamp"],
                    tags=r["tags"] or "",
                    content=r["content"] or "",
                )
            )
    return out


def _neighbours(
    conn: sqlite3.Connection, anchor: _RegionEntry, top_k: int = 5
) -> list[_RegionEntry]:
    if not anchor.content.strip():
        return []
    # DELIBERATELY search_hybrid, not the associative entrance (§5 cutover): this is
    # a near-duplicate probe with a whole entry BODY as the query, not a question —
    # distilling it would fire the entity slot on every name the body mentions and
    # spread activation where similarity is the only thing being asked.
    hits = fts.search_hybrid(conn, query=anchor.content, top_k=top_k, include_superseded=False)
    out: list[_RegionEntry] = []
    for h in hits:
        if h.id == anchor.id:
            continue
        out.append(
            _RegionEntry(
                id=h.id,
                path=h.path,
                timestamp=h.timestamp,
                tags="",  # FTS hits don't carry the tags column; not needed here
                content=h.content,
            )
        )
    return out


def _assemble_region(
    conn: sqlite3.Connection,
    session_ids: list[str],
    max_size: int,
) -> list[_RegionEntry]:
    anchors = _session_entries(conn, session_ids)
    if not anchors:
        return []
    seen: dict[str, _RegionEntry] = {a.id: a for a in anchors}
    for a in anchors:
        for n in _neighbours(conn, a, top_k=5):
            if n.id not in seen:
                seen[n.id] = n
            if len(seen) >= max_size:
                break
        if len(seen) >= max_size:
            break
    region = list(seen.values())
    region.sort(key=lambda e: e.timestamp)
    return region[:max_size]


def _render_region(region: list[_RegionEntry]) -> str:
    parts: list[str] = ["## Working region", ""]
    for e in region:
        parts.append(f"### {e.path} {{id: {e.id}}} [{e.timestamp}]")
        body = (e.content or "").strip()
        if body:
            parts.append(body)
        parts.append("")
    return "\n".join(parts).rstrip()


# ─── dispatch with dry-run interception ──────────────────────────────────


def _make_dispatch(
    conn: sqlite3.Connection,
    cfg: Config,
    state: tools_mod.CommitState,
    *,
    dry_run: bool,
    superseded_ids: list[str],
    planned_ops: list[dict[str, Any]],
) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    """Return a dispatch fn closing over conn/state and the dry-run shims."""

    def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "inspect_source":
            return tool_inspect_source(conn, entry_id=str(args.get("entry_id") or ""))

        if name == "supersede":
            old_id = str(args.get("old_entry_id") or "")
            superseded_ids.append(old_id)
            if dry_run:
                planned_ops.append(
                    {
                        "op": "supersede",
                        "path": str(args.get("path") or ""),
                        "old_entry_id": old_id,
                        "new_content": str(args.get("new_content") or ""),
                        "reason": str(args.get("reason") or ""),
                        "tags": list(args.get("tags") or []),
                    }
                )
                state.written_ids.append(f"planned:supersede:{old_id}")
                return {"ok": True, "dry_run": True, "new_id": f"planned:{old_id}"}

        if name == "append" and dry_run:
            planned_ops.append(
                {
                    "op": "append",
                    "path": str(args.get("path") or ""),
                    "content": str(args.get("content") or ""),
                    "tags": list(args.get("tags") or []),
                }
            )
            planned_id = f"planned:append:{len(planned_ops)}"
            state.written_ids.append(planned_id)
            return {"ok": True, "dry_run": True, "id": planned_id}

        # Fall through to the standard writer dispatch — read_memory,
        # search_memory, commit, and non-dry-run supersede/append.
        return tools_mod.dispatch(
            name,
            args,
            conn=conn,
            soft_limit_tokens=cfg.writer.soft_limit_tokens,
            state=state,
        )

    return dispatch


# ─── public entry point ─────────────────────────────────────────────────


def consolidate_region(
    cfg: Config,
    session_ids: list[str],
    *,
    dry_run: bool = False,
) -> ConsolidateResult:
    """Run cross-file consolidation over the working region for ``session_ids``.

    Returns a ``ConsolidateResult``. On the early-empty path
    (``no anchors found``) the result has ``committed=False`` and
    ``skipped_reason`` set; the LLM is not invoked.
    """
    session_ids = [s for s in session_ids if s]
    if not session_ids:
        return ConsolidateResult(
            triggered_by_sessions=[],
            committed=False,
            skipped_reason="no session ids provided",
            dry_run=dry_run,
        )

    with fts.cursor() as conn:
        entries_mod.write_preset_files(conn)
        region = _assemble_region(
            conn,
            session_ids,
            max_size=cfg.writer.consolidation_max_region_size,
        )
        if not region:
            logger.info(
                "consolidate: empty region (no entries for sessions %s)",
                ",".join(session_ids),
            )
            return ConsolidateResult(
                triggered_by_sessions=list(session_ids),
                committed=False,
                skipped_reason="no entries in working region",
                dry_run=dry_run,
            )

        return _run_consolidator_loop(
            cfg,
            conn,
            session_ids=session_ids,
            region=region,
            dry_run=dry_run,
        )


def _run_consolidator_loop(
    cfg: Config,
    conn: sqlite3.Connection,
    *,
    session_ids: list[str],
    region: list[_RegionEntry],
    dry_run: bool,
) -> ConsolidateResult:
    system = load_prompt("consolidator.md")
    schema = load_prompt("schema.md")
    region_text = _render_region(region)

    user_msg = (
        f"# Schema\n\n{schema}\n\n"
        f"# Trigger sessions\n\n{', '.join(session_ids)}\n\n"
        f"# Working region ({len(region)} entries)\n\n"
        f"{region_text}\n\n"
        "Use `inspect_source(entry_id=...)` on any entry whose wording is "
        "ambiguous before rewriting it. Use `supersede` and `append` per "
        "the rules in your system prompt. Call `commit(summary)` exactly "
        "once when done."
    )
    if dry_run:
        user_msg += (
            "\n\n**Dry-run mode**: your supersede/append calls will be "
            "recorded as planned operations but not written to disk."
        )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]

    state = tools_mod.CommitState()
    superseded_ids: list[str] = []
    planned_ops: list[dict[str, Any]] = []

    dispatch_fn = _make_dispatch(
        conn,
        cfg,
        state,
        dry_run=dry_run,
        superseded_ids=superseded_ids,
        planned_ops=planned_ops,
    )

    iters = llm_mod.run_tool_loop(
        cfg,
        cfg.writer.consolidation_stage,
        messages,
        tools=CONSOLIDATOR_SCHEMAS,
        dispatch_fn=functools.partial(_call_dispatch, dispatch_fn=dispatch_fn),
        valid_tool_names=CONSOLIDATOR_TOOL_NAMES,
        state=state,
        max_iter=cfg.writer.consolidation_max_iterations,
        log_tag=f"consolidator {','.join(session_ids)}",
    )

    return ConsolidateResult(
        triggered_by_sessions=list(session_ids),
        committed=state.committed,
        summary=state.summary,
        superseded_ids=list(superseded_ids),
        written_ids=list(state.written_ids),
        iterations=iters,
        dry_run=dry_run,
        region_size=len(region),
        planned_ops=list(planned_ops),
    )


def _call_dispatch(
    name: str,
    args: dict[str, Any],
    *,
    dispatch_fn: Callable[[str, dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Adapter so ``functools.partial`` exposes the (name, args) signature.

    ``run_tool_loop`` calls ``dispatch_fn(name, args)``; closing over
    ``dispatch`` directly works too but ``partial`` keeps the call site
    introspectable for tests.
    """
    return dispatch_fn(name, args)
