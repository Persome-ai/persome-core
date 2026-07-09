"""On-disk chat history: paths, active/archived sessions, and read-only queries.

Extracted from `chat/handler.py` so that `chat.tool_handlers.tool_search_chat_history`
and `tool_list_chat_sessions` can depend on it directly instead of reaching
back into the handler module via a function-body late import to a private symbol.
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import paths


def history_dir() -> Path:
    d = paths.root() / "chat-history"
    d.mkdir(parents=True, exist_ok=True)
    return d


def active_path() -> Path:
    """Current session's history file."""
    return history_dir() / "active.json"


def archive_path(session_id: str) -> Path:
    return history_dir() / f"{session_id}.json"


def save_history(messages: list[dict[str, Any]]) -> None:
    """Save current messages to active session file. Skip system prompts."""
    saveable = [m for m in messages if m.get("role") != "system"]
    with contextlib.suppress(OSError):
        active_path().write_text(json.dumps(saveable, ensure_ascii=False, default=str, indent=2))


def load_history() -> list[dict[str, Any]]:
    """Load previous chat history from the active session."""
    p = active_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        if isinstance(data, list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return []


def archive_current() -> str | None:
    """Archive the active session with a timestamp-based filename. Returns archive name."""
    p = active_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        if not isinstance(data, list) or not data:
            p.unlink(missing_ok=True)
            return None
    except (OSError, json.JSONDecodeError):
        p.unlink(missing_ok=True)
        return None
    session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = archive_path(session_id)
    p.rename(archive)
    return session_id


def search_chat_history(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search across all archived chat sessions for matching messages."""
    query_lower = query.lower()
    results: list[dict[str, Any]] = []
    hist = history_dir()

    # Search all session files (archived + active), newest first
    files = sorted(hist.glob("*.json"), reverse=True)
    for f in files:
        try:
            data = json.loads(f.read_text())
            if not isinstance(data, list):
                continue
        except (OSError, json.JSONDecodeError):
            continue

        session_name = f.stem
        for m in data:
            content = m.get("content") or ""
            role = m.get("role", "")
            if role in ("tool",):
                continue
            if query_lower in content.lower():
                results.append(
                    {
                        "session": session_name,
                        "role": role,
                        "content": content[:500],
                    }
                )
                if len(results) >= limit:
                    return results
    return results


def list_chat_sessions() -> list[dict[str, Any]]:
    """List all chat sessions with summary info."""
    hist = history_dir()
    sessions: list[dict[str, Any]] = []
    for f in sorted(hist.glob("*.json"), reverse=True):
        try:
            data = json.loads(f.read_text())
            if not isinstance(data, list):
                continue
        except (OSError, json.JSONDecodeError):
            continue
        user_msgs = [m for m in data if m.get("role") == "user"]
        first_msg = user_msgs[0]["content"][:100] if user_msgs else ""
        sessions.append(
            {
                "session": f.stem,
                "turns": len(user_msgs),
                "first_message": first_msg,
            }
        )
    return sessions[:20]
