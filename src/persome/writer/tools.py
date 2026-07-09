"""Writer tool implementations + JSON Schema declarations for the LLM.

写权反转（PR-6b，SSOT 切换设计 §1.3/§5）：本模块是 classifier / dream /
pattern_detector / consolidator 共用的写工具层；``write_authority="evomem"`` 时
``tool_create``/``tool_append``/``tool_supersede`` 经 ``store/entries.py`` 的
choke-point dispatch 走 evomem engine 落 evo_nodes，markdown 由投影器再生成
（``tool_flag_compact`` 改走 ``inversion.flag_needs_compact``——files 行是真相，
重投影替代直接 ``update_frontmatter``，避免投影态 hash 失配触发手改误报）。
写 op 由各 stage 的 LLM agent 决定（dream 的 update_memory = 已决定的
supersede），engine 不再重新决策——reconcile 调和升级与反转解耦。逐站输出
等价由 ``tests/test_evomem/test_inversion_stations.py`` 钉死。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..evomem import inversion as evo_inversion
from ..logger import get
from ..store import entries as entries_mod
from ..store import files as files_mod
from ..store import fts
from .chat_extractor import extract_chat_messages

logger = get("persome.writer")


@dataclass
class CommitState:
    committed: bool = False
    summary: str = ""
    written_ids: list[str] = field(default_factory=list)
    created_paths: list[str] = field(default_factory=list)
    flagged_compact: list[str] = field(default_factory=list)


def tool_read_memory(conn: sqlite3.Connection, *, path: str, tail_n: int = 10) -> dict[str, Any]:
    p = files_mod.memory_path(path)
    if not p.exists():
        return {"error": f"file not found: {path}"}
    parsed = files_mod.read_file(p)
    tail = parsed.entries[-tail_n:] if tail_n > 0 else parsed.entries
    return {
        "path": path,
        "description": parsed.description,
        "tags": parsed.tags,
        "status": parsed.status,
        "entry_count": parsed.entry_count,
        "updated": parsed.updated,
        "entries": [
            {
                "id": e.id,
                "timestamp": e.timestamp,
                "tags": e.tags,
                "body": e.body,
                "superseded_by": e.superseded_by,
                "confidence": e.confidence,
                "conflicted": e.conflicted,
                "occurred_at": e.occurred_at,
            }
            for e in tail
        ],
    }


def tool_search_memory(
    conn: sqlite3.Connection,
    *,
    query: str,
    top_k: int = 5,
    include_superseded: bool = False,
    path_prefix: str | None = None,
) -> dict[str, Any]:
    path_patterns = [f"{path_prefix}*.md"] if path_prefix else None
    if include_superseded:
        hits = fts.search_hybrid(
            conn,
            query=query,
            top_k=top_k,
            include_superseded=True,
            path_patterns=path_patterns,
        )
    else:
        # §5 read cutover (same single choke point as MCP/chat)
        from ..retrieval import associative as assoc_mod

        hits = assoc_mod.associative_read(
            conn, query=query, top_k=top_k, path_patterns=path_patterns
        )
    return {
        "query": query,
        "results": [
            {
                "id": h.id,
                "path": h.path,
                "timestamp": h.timestamp,
                "content": h.content,
                "rank": h.rank,
            }
            for h in hits
        ],
    }


def tool_append(
    conn: sqlite3.Connection,
    *,
    path: str,
    content: str,
    tags: list[str],
    soft_limit_tokens: int,
    state: CommitState,
    confidence: str | None = None,
    conflicted: bool = False,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    try:
        entry_id = entries_mod.append_entry(
            conn,
            name=path,
            content=content,
            tags=tags,
            soft_limit_tokens=soft_limit_tokens,
            confidence=confidence,
            conflicted=conflicted,
            occurred_at=occurred_at,
        )
    except (FileNotFoundError, ValueError) as exc:
        return {"error": str(exc)}
    state.written_ids.append(entry_id)
    return {"ok": True, "id": entry_id, "path": path}


def tool_create(
    conn: sqlite3.Connection,
    *,
    path: str,
    description: str,
    tags: list[str],
    state: CommitState,
) -> dict[str, Any]:
    try:
        entries_mod.create_file(conn, name=path, description=description, tags=tags)
    except (FileExistsError, ValueError) as exc:
        return {"error": str(exc)}
    state.created_paths.append(path)
    return {"ok": True, "path": path}


def tool_supersede(
    conn: sqlite3.Connection,
    *,
    path: str,
    old_entry_id: str,
    new_content: str,
    reason: str,
    tags: list[str] | None,
    state: CommitState,
    confidence: str | None = None,
    conflicted: bool = False,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    try:
        new_id = entries_mod.supersede_entry(
            conn,
            name=path,
            old_entry_id=old_entry_id,
            new_content=new_content,
            reason=reason,
            tags=tags,
            confidence=confidence,
            conflicted=conflicted,
            occurred_at=occurred_at,
        )
    except (FileNotFoundError, ValueError) as exc:
        return {"error": str(exc)}
    state.written_ids.append(new_id)
    return {"ok": True, "new_id": new_id}


def tool_flag_compact(
    conn: sqlite3.Connection, *, path: str, reason: str, state: CommitState
) -> dict[str, Any]:
    p = files_mod.memory_path(path)
    if not p.exists():
        return {"error": f"file not found: {path}"}
    if evo_inversion.routes_to_engine(path):
        # 反转模式：files 行是文件级元数据的真相，frontmatter 由重投影带回——
        # 直接 update_frontmatter 会让盘上文件偏离 projection_state 的 hash，
        # 被手改检测误报。
        evo_inversion.flag_needs_compact(conn, name=path, value=True)
    else:
        fts.set_needs_compact(conn, p.name, True)
        files_mod.update_frontmatter(p, {"needs_compact": True})
    state.flagged_compact.append(path)
    logger.info("flag_compact: %s (%s)", path, reason)
    return {"ok": True}


def tool_commit(state: CommitState, *, summary: str) -> dict[str, Any]:
    state.committed = True
    state.summary = summary
    return {"ok": True}


# ─── JSON Schema declarations (OpenAI tool format) ───────────────────────

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_memory",
            "description": "Read a memory file (frontmatter + last N entries).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "e.g. 'project-persome.md'"},
                    "tail_n": {"type": "integer", "default": 10},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": (
                "BM25 full-text search across all memory. Use to dedup before appending. "
                "Optionally restrict to files matching a prefix (e.g. 'person-', 'project-') "
                "for targeted entity lookups."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "default": 5},
                    "include_superseded": {"type": "boolean", "default": False},
                    "path_prefix": {
                        "type": "string",
                        "description": (
                            "Restrict search to files whose name starts with this prefix, "
                            "e.g. 'person-' or 'project-'. Omit to search all files."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append",
            "description": "Append a new entry to a memory file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {
                        "type": "string",
                        "description": "1–3 sentence self-contained fact",
                    },
                    "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                        "description": (
                            "How reliable this memory is. high = the user explicitly did/said it; "
                            "medium = strong inference from evidence; low = weak/speculative "
                            "inference. Omit only when truly unsure."
                        ),
                    },
                    "conflicted": {
                        "type": "boolean",
                        "description": (
                            "Set true when this contradicts an existing memory but you are NOT "
                            "confident which one is right — surfaces the conflict instead of "
                            "hard-overwriting. Prefer this over supersede when unsure."
                        ),
                    },
                    "occurred_at": {
                        "type": "string",
                        "description": (
                            "ISO-8601 time the underlying event actually happened, if it differs "
                            "from now (e.g. carried from the source event). Omit if it's current."
                        ),
                    },
                },
                "required": ["path", "content", "tags"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create",
            "description": (
                "Create a new memory file. Filename prefix must be one of: "
                "user-, project-, tool-, topic-, person-, org-, event-, skill-. For skill drafts detected by pattern analysis, use path skills/skill-{slug}.md with stage: draft in the entry body."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "description": {
                        "type": "string",
                        "description": "One-line description; required",
                    },
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["path", "description", "tags"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "supersede",
            "description": "Mark an old entry as superseded and append the replacement.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_entry_id": {"type": "string"},
                    "new_content": {"type": "string"},
                    "reason": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                        "description": "Reliability of the replacement entry (see append).",
                    },
                    "conflicted": {
                        "type": "boolean",
                        "description": "Mark the replacement as still-contested (rare on supersede).",
                    },
                    "occurred_at": {
                        "type": "string",
                        "description": "ISO-8601 event time of the replacement, if not current.",
                    },
                },
                "required": ["path", "old_entry_id", "new_content", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "flag_compact",
            "description": "Flag a file for the next compaction pass.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["path", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "commit",
            "description": "Finish this round. Call exactly once at the end.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "One-line summary of what you wrote.",
                    },
                },
                "required": ["summary"],
            },
        },
    },
]


TOOL_NAMES = {t["function"]["name"] for t in TOOL_SCHEMAS}



# Re-exported for callers that need to know which tools are concurrency-safe
# without importing from llm.py (avoid circular deps).
CONCURRENCY_SAFE_TOOLS: frozenset[str] = frozenset(
    {
        "read_memory",
        "search_memory",
        "drill_capture",
        "drill_window",
        "drill_chat",
        "drill_chat_captures",
        "drill_timeline",
    }
)


def _validate_tool_args(name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """Validate tool args with Pydantic. Returns error dict on failure, None if valid."""
    try:
        from .tools_schema import TOOL_INPUT_MODELS

        model_cls = TOOL_INPUT_MODELS.get(name)
        if model_cls is None:
            return None
        model_cls.model_validate(args)
        return None
    except Exception as exc:  # noqa: BLE001
        return {"error": f"invalid input: {exc}"}


def dispatch(
    name: str,
    args: dict[str, Any],
    *,
    conn: sqlite3.Connection,
    soft_limit_tokens: int,
    state: CommitState,
) -> dict[str, Any]:
    err = _validate_tool_args(name, args)
    if err is not None:
        return err
    if name == "read_memory":
        return tool_read_memory(conn, path=args["path"], tail_n=args.get("tail_n", 10))
    if name == "search_memory":
        return tool_search_memory(
            conn,
            query=args["query"],
            top_k=args.get("top_k", 5),
            include_superseded=args.get("include_superseded", False),
            path_prefix=args.get("path_prefix"),
        )
    if name == "append":
        return tool_append(
            conn,
            path=args["path"],
            content=args["content"],
            tags=list(args.get("tags", []) or []),
            soft_limit_tokens=soft_limit_tokens,
            state=state,
            confidence=args.get("confidence"),
            conflicted=bool(args.get("conflicted", False)),
            occurred_at=args.get("occurred_at"),
        )
    if name == "create":
        return tool_create(
            conn,
            path=args["path"],
            description=args["description"],
            tags=list(args.get("tags", []) or []),
            state=state,
        )
    if name == "supersede":
        return tool_supersede(
            conn,
            path=args["path"],
            old_entry_id=args["old_entry_id"],
            new_content=args["new_content"],
            reason=args["reason"],
            tags=list(args.get("tags") or []) or None,
            state=state,
            confidence=args.get("confidence"),
            conflicted=bool(args.get("conflicted", False)),
            occurred_at=args.get("occurred_at"),
        )
    if name == "flag_compact":
        return tool_flag_compact(
            conn, path=args["path"], reason=args.get("reason", ""), state=state
        )
    if name == "commit":
        return tool_commit(state, summary=args.get("summary", ""))
    if name == "drill_chat_captures":
        return tool_drill_chat_captures(
            conn,
            app_name=str(args.get("app_name") or ""),
            start_ts=str(args.get("start_ts") or ""),
            end_ts=str(args.get("end_ts") or ""),
            max_bytes=int(args.get("max_bytes", 12_000) or 12_000),
        )
    return {"error": f"unknown tool: {name}"}


# Chat-history filenames are wall-clock minted by the chat layer:
# YYYYMMDD-HHMMSS.json. Validate before joining onto the chat-history
# directory so a hostile filename like "../etc/passwd" can never escape.
_CHAT_FILENAME_RE = re.compile(r"^\d{8}-\d{6}\.json$")


def tool_drill_capture(
    conn: sqlite3.Connection,
    *,
    capture_id: str,
    text_limit: int = 2000,
) -> dict[str, Any]:
    """Fetch one capture row with its full visible_text (truncated)."""
    if not capture_id or not isinstance(capture_id, str):
        return {"error": "capture_id required"}
    row = conn.execute(
        "SELECT id, timestamp, app_name, bundle_id, window_title, "
        "       focused_role, focused_value, url, visible_text "
        "  FROM captures WHERE id=?",
        (capture_id,),
    ).fetchone()
    if row is None:
        return {"error": f"capture not found: {capture_id}"}
    text = (row["visible_text"] or "")[: max(0, int(text_limit))]
    return {
        "id": row["id"],
        "timestamp": row["timestamp"],
        "app_name": row["app_name"] or "",
        "bundle_id": row["bundle_id"] or "",
        "window_title": row["window_title"] or "",
        "focused_role": row["focused_role"] or "",
        "focused_value": row["focused_value"] or "",
        "url": row["url"] or "",
        "visible_text": text,
        "truncated": len(row["visible_text"] or "") > len(text),
    }


def tool_drill_chat_captures(
    conn: sqlite3.Connection,
    *,
    app_name: str,
    start_ts: str,
    end_ts: str,
    max_bytes: int = 12_000,
) -> dict[str, Any]:
    """Reconstruct a chat conversation from captures for a given app and time range.

    Returns timestamped visible_text snapshots with scroll-gap markers where the
    user scrolled fast enough that content may have been missed between captures.
    """
    text, snapshot_count, gap_count = extract_chat_messages(
        conn, app_name, start_ts, end_ts, max_bytes
    )
    if not text:
        return {
            "ok": False,
            "app_name": app_name,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "snapshot_count": 0,
            "gap_count": 0,
            "content": "",
            "message": "no captures found for this app/time range",
        }
    return {
        "ok": True,
        "app_name": app_name,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "snapshot_count": snapshot_count,
        "gap_count": gap_count,
        "content": text,
    }


def tool_drill_window(
    conn: sqlite3.Connection,
    *,
    title: str | None = None,
    url: str | None = None,
    app_name: str | None = None,
    since_days: int = 30,
    limit: int = 20,
    text_preview: int = 200,
) -> dict[str, Any]:
    """List captures matching a window title / URL / app substring.

    At least one of title, url, app_name must be set. Returns up to ``limit``
    rows, newest-first, each with a ``visible_text`` preview clipped to
    ``text_preview`` chars so the whole response stays bounded.
    """
    if not any([title, url, app_name]):
        return {"error": "one of title, url, app_name is required"}

    since = (datetime.now().astimezone() - timedelta(days=max(1, int(since_days)))).isoformat()
    clauses: list[str] = ["timestamp >= ?"]
    args: list[Any] = [since]
    if title:
        clauses.append("LOWER(window_title) LIKE ?")
        args.append(f"%{title.lower()}%")
    if url:
        clauses.append("LOWER(url) LIKE ?")
        args.append(f"%{url.lower()}%")
    if app_name:
        clauses.append("LOWER(app_name) LIKE ?")
        args.append(f"%{app_name.lower()}%")

    sql = (
        "SELECT id, timestamp, app_name, window_title, focused_role, "
        "       focused_value, url, visible_text "
        "  FROM captures WHERE " + " AND ".join(clauses) + " "
        "ORDER BY timestamp DESC LIMIT ?"
    )
    args.append(max(1, int(limit)))
    rows = conn.execute(sql, args).fetchall()

    preview = max(0, int(text_preview))
    results = []
    for r in rows:
        full = r["visible_text"] or ""
        results.append(
            {
                "id": r["id"],
                "timestamp": r["timestamp"],
                "app_name": r["app_name"] or "",
                "window_title": r["window_title"] or "",
                "focused_role": r["focused_role"] or "",
                "focused_value": r["focused_value"] or "",
                "url": r["url"] or "",
                "visible_text_preview": full[:preview],
            }
        )
    return {"count": len(results), "captures": results}


def tool_drill_chat(
    *,
    file: str,
    max_messages: int = 20,
    text_limit_per_msg: int = 400,
) -> dict[str, Any]:
    """Read up to ``max_messages`` messages from one chat-history JSON.

    The filename MUST match ``YYYYMMDD-HHMMSS.json`` (no path separators,
    no traversal) — otherwise this errors before touching the filesystem.
    """
    if not isinstance(file, str) or not _CHAT_FILENAME_RE.match(file):
        return {"error": f"invalid chat file (must match YYYYMMDD-HHMMSS.json): {file!r}"}

    from .. import paths as paths_mod

    history_dir = paths_mod.root() / "chat-history"
    p = history_dir / file
    if not p.exists() or not p.is_file():
        return {"error": f"chat file not found: {file}"}
    # Defense in depth — the regex already prevents this, but verify the
    # resolved path is still under history_dir.
    try:
        p.resolve().relative_to(history_dir.resolve())
    except ValueError:
        return {"error": f"chat file outside history dir: {file}"}

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"error": f"unreadable chat file: {exc}"}
    if not isinstance(data, list):
        return {"error": "chat file is not a JSON array"}

    cap_n = max(1, int(max_messages))
    clip = max(0, int(text_limit_per_msg))
    out = []
    for msg in data[:cap_n]:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", ""))
        content = msg.get("content")
        text = ""
        if isinstance(content, str):
            text = content[:clip]
        elif isinstance(content, list):
            # OpenAI tool-call content arrays — flatten the text parts.
            parts: list[str] = []
            for c in content:
                if isinstance(c, dict) and isinstance(c.get("text"), str):
                    parts.append(c["text"])
            text = " ".join(parts)[:clip]
        tool_calls = msg.get("tool_calls")
        actions: list[dict[str, str]] = []
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                func = tc.get("function") or {}
                actions.append(
                    {
                        "tool": str(func.get("name", "")),
                        "args": str(func.get("arguments", ""))[:clip],
                    }
                )
        out.append({"role": role, "content": text, "tool_calls": actions})
    return {"file": file, "count": len(out), "messages": out}


def tool_drill_timeline(
    conn: sqlite3.Connection,
    *,
    date: str,
    limit: int = 60,
) -> dict[str, Any]:
    """Return all 1-min timeline blocks for one local date (YYYY-MM-DD)."""
    try:
        day = datetime.strptime(date, "%Y-%m-%d").astimezone()
    except (TypeError, ValueError):
        return {"error": f"invalid date (want YYYY-MM-DD): {date!r}"}

    from ..timeline import store as timeline_store

    next_day = day + timedelta(days=1)
    blocks = timeline_store.query_since(conn, day)
    cap_n = max(1, int(limit))
    out = []
    for b in blocks:
        if b.start_time >= next_day:
            continue
        out.append(
            {
                "id": b.id,
                "start": b.start_time.isoformat(),
                "end": b.end_time.isoformat(),
                "apps": list(b.apps_used or []),
                "capture_count": b.capture_count,
                "entries": list(b.entries or []),
            }
        )
        if len(out) >= cap_n:
            break
    return {"date": date, "count": len(out), "blocks": out}


# ─── dream tool registry ─────────────────────────────────────────────────

DRILL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "drill_capture",
            "description": (
                "Read one capture row by id, including its full visible_text "
                "(truncated to text_limit). Use to ground a candidate in the "
                "exact UI/content the user saw at that moment."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "capture_id": {"type": "string"},
                    "text_limit": {"type": "integer", "default": 2000},
                },
                "required": ["capture_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drill_window",
            "description": (
                "List captures matching a window title / URL / app substring "
                "(at least one filter required). Use to find every time a "
                "candidate page/form was visited and pick capture_ids to "
                "drill into."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "app_name": {"type": "string"},
                    "since_days": {"type": "integer", "default": 30},
                    "limit": {"type": "integer", "default": 20},
                    "text_preview": {"type": "integer", "default": 200},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drill_chat",
            "description": (
                "Read up to N messages from one chat-history JSON. "
                "Filename must match YYYYMMDD-HHMMSS.json."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "max_messages": {"type": "integer", "default": 20},
                    "text_limit_per_msg": {"type": "integer", "default": 400},
                },
                "required": ["file"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drill_timeline",
            "description": (
                "Return 1-min timeline blocks for one local date (YYYY-MM-DD). "
                "Use to inspect the actual minute-by-minute activity of a "
                "representative day for a candidate routine."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "limit": {"type": "integer", "default": 60},
                },
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drill_chat_captures",
            "description": (
                "Reconstruct a chat conversation from raw screen captures for a given "
                "app (e.g. '飞书', 'WeChat', 'Messages') between two ISO-8601 timestamps. "
                "Returns timestamped visible_text snapshots with ⚠️ scroll-gap markers "
                "where content may be missing. Use when classifying a session with "
                "significant chat app activity to extract durable facts from the "
                "conversation (decisions, action items, contacts, project topics)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "Exact app_name as captured (e.g. '飞书', 'WeChat')",
                    },
                    "start_ts": {
                        "type": "string",
                        "description": "ISO-8601 start timestamp (inclusive)",
                    },
                    "end_ts": {
                        "type": "string",
                        "description": "ISO-8601 end timestamp (inclusive)",
                    },
                    "max_bytes": {
                        "type": "integer",
                        "default": 12000,
                        "description": "Max bytes of output; most recent content kept on truncation",
                    },
                },
                "required": ["app_name", "start_ts", "end_ts"],
            },
        },
    },
]


# Dream gets every write tool (read_memory / search_memory / append /
# create / supersede / flag_compact / commit) plus all drills.
DREAM_SCHEMAS: list[dict[str, Any]] = TOOL_SCHEMAS + DRILL_SCHEMAS
DREAM_TOOL_NAMES = {t["function"]["name"] for t in DREAM_SCHEMAS}

# Classifier gets write tools plus drill_chat_captures (to extract chat content
# from sessions involving chat apps like Feishu / WeChat).
_DRILL_CHAT_CAPTURES_SCHEMA = next(
    s for s in DRILL_SCHEMAS if s["function"]["name"] == "drill_chat_captures"
)
CLASSIFIER_SCHEMAS: list[dict[str, Any]] = TOOL_SCHEMAS + [_DRILL_CHAT_CAPTURES_SCHEMA]
CLASSIFIER_TOOL_NAMES = {t["function"]["name"] for t in CLASSIFIER_SCHEMAS}


def dispatch_dream(
    name: str,
    args: dict[str, Any],
    *,
    conn: sqlite3.Connection,
    soft_limit_tokens: int,
    state: CommitState,
) -> dict[str, Any]:
    """Route a dream tool call: drills first, then fall through to writers."""
    err = _validate_tool_args(name, args)
    if err is not None:
        return err
    if name == "drill_capture":
        return tool_drill_capture(
            conn,
            capture_id=str(args.get("capture_id") or ""),
            text_limit=int(args.get("text_limit", 2000) or 2000),
        )
    if name == "drill_window":
        return tool_drill_window(
            conn,
            title=args.get("title") or None,
            url=args.get("url") or None,
            app_name=args.get("app_name") or None,
            since_days=int(args.get("since_days", 30) or 30),
            limit=int(args.get("limit", 20) or 20),
            text_preview=int(args.get("text_preview", 200) or 200),
        )
    if name == "drill_chat":
        return tool_drill_chat(
            file=str(args.get("file") or ""),
            max_messages=int(args.get("max_messages", 20) or 20),
            text_limit_per_msg=int(args.get("text_limit_per_msg", 400) or 400),
        )
    if name == "drill_timeline":
        return tool_drill_timeline(
            conn,
            date=str(args.get("date") or ""),
            limit=int(args.get("limit", 60) or 60),
        )
    if name == "drill_chat_captures":
        return tool_drill_chat_captures(
            conn,
            app_name=str(args.get("app_name") or ""),
            start_ts=str(args.get("start_ts") or ""),
            end_ts=str(args.get("end_ts") or ""),
            max_bytes=int(args.get("max_bytes", 12_000) or 12_000),
        )
    # Fall through to the existing write toolset.
    return dispatch(
        name,
        args,
        conn=conn,
        soft_limit_tokens=soft_limit_tokens,
        state=state,
    )
