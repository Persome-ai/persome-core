"""Book-chapter generation: cluster recent chat sessions into themed chapters.

The Book → Sessions list groups chat sessions into literary *chapters*. This
module owns the one LLM step that does the clustering + titling:

1. :func:`recent_sessions` — read the recent non-archived chat sessions off disk
   (``chat-history/api-*.json``) as lightweight ``{id, title, preview}`` rows.
2. :func:`cluster_chapters` — ask the model to group those rows into 0–N themed
   chapters, each with a literary title + the backing ``session_ids``.

Persistence is :mod:`persome.store.book_chapters`; orchestration is
:func:`run_book_chapters`, hung off the daily Dream run (after the book-page
sub-step). The whole run is fault-tolerant — a chapter-generation failure must
never break the Dream run it hangs off.

``call_llm`` is imported into module scope so tests can monkeypatch
``book_chapters.call_llm`` directly.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .. import paths
from ..config import Config
from ..config import load as load_config
from ..logger import get
from ..prompts import load as load_prompt
from ..store import book_chapters as chapters_store
from ..store import fts
from .llm import OnEventFn, call_llm, extract_text

logger = get("persome.writer")

_STAGE = "book_chapters"

# How many of the most-recently-updated sessions to feed the clusterer.
_RECENT_WINDOW = 30
# Trim each session preview so the prompt stays compact.
_PREVIEW_MAX_CHARS = 120

# Match the first top-level JSON array in a possibly-chatty LLM reply.
_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)
_CURRENT_TIME_PREFIX_RE = re.compile(r"^\[Current time: [^\]]+\]\n\n")


def _extract_json_array(text: str) -> list[Any]:
    """Parse the first ``[...]`` array out of ``text``; ``[]`` on any failure."""
    span = _ARRAY_RE.search(text)
    for candidate in (text, span.group(0) if span else None):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, list):
            return parsed
    return []


def _history_dir() -> Path:
    return paths.root() / "chat-history"


def _content_to_text(content: Any) -> str:
    """Collapse a stored message ``content`` field to a plain string."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "".join(parts)
    return str(content)


def _first_user_preview(messages: list[dict[str, Any]]) -> str:
    """Short preview from the first real user turn; '' when there is none."""
    for m in messages:
        if m.get("role") != "user":
            continue
        text = _content_to_text(m.get("content"))
        if not text:
            continue
        text = _CURRENT_TIME_PREFIX_RE.sub("", text, count=1)
        cleaned = " ".join(text.split())
        if not cleaned:
            continue
        if len(cleaned) > _PREVIEW_MAX_CHARS:
            return cleaned[:_PREVIEW_MAX_CHARS].rstrip() + "…"
        return cleaned
    return ""


def recent_sessions(limit: int = _RECENT_WINDOW) -> list[dict[str, str]]:
    """Read recent non-archived chat sessions as ``{id, title, preview}`` rows.

    Reads ``chat-history/api-*.json`` directly (the writer must not depend on the
    API module's in-process session cache). Sessions explicitly flagged
    ``archived: true`` on disk are skipped. Newest-updated first, capped at
    ``limit``. Malformed files are skipped silently.
    """
    hist = _history_dir()
    if not hist.exists():
        return []

    rows: list[tuple[str, dict[str, str]]] = []
    for p in hist.glob("api-*.json"):
        sid = p.stem[len("api-") :]
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if bool(data.get("archived", False)):
            continue
        messages = data.get("messages", [])
        if not isinstance(messages, list):
            messages = []
        # Skip empty sessions — a chapter must group sessions with real content.
        if not any(isinstance(m, dict) and m.get("role") == "user" for m in messages):
            continue
        title = data.get("title")
        title = title.strip() if isinstance(title, str) and title.strip() else ""
        updated_at = str(data.get("updated_at") or data.get("created_at") or "")
        rows.append(
            (
                updated_at,
                {"id": sid, "title": title, "preview": _first_user_preview(messages)},
            )
        )

    rows.sort(key=lambda r: r[0], reverse=True)
    return [row for _, row in rows[:limit]]


def _render_session_block(sessions: list[dict[str, str]]) -> str:
    lines: list[str] = []
    for s in sessions:
        title = s["title"] or "(untitled)"
        preview = s["preview"] or "(no preview)"
        lines.append(f"- id: {s['id']}\n  title: {title}\n  preview: {preview}")
    return "\n".join(lines)


def cluster_chapters(sessions: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Cluster sessions into 0–N themed chapters via the LLM.

    Returns a list of ``{"title": str, "subtitle": str, "session_ids": list[str]}``.
    Every returned ``session_id`` is guaranteed to be one of the input session
    ids (the model is told never to invent ids, but we enforce it here too — a
    chapter must never claim a session it can't open). Chapters left with no
    valid ids after filtering are dropped. Empty input → ``[]``.
    """
    if not sessions:
        return []

    known = {s["id"] for s in sessions}
    system = load_prompt("book_chapters.md")
    user = "# Recent chat sessions\n\n" + _render_session_block(sessions)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    resp = call_llm(_cfg(), _STAGE, messages=messages)
    text = extract_text(resp)

    chapters: list[dict[str, Any]] = []
    for item in _extract_json_array(text):
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        raw_ids = item.get("session_ids")
        ids = [str(x) for x in raw_ids if str(x) in known] if isinstance(raw_ids, list) else []
        if not ids:
            continue  # never store a chapter the reader can't open
        subtitle = item.get("subtitle")
        chapters.append(
            {
                "title": title.strip(),
                "subtitle": subtitle.strip() if isinstance(subtitle, str) else "",
                "session_ids": ids,
            }
        )
    return chapters


def run_book_chapters(*, on_event: OnEventFn | None = None) -> int:
    """Regenerate book chapters from recent sessions. Returns rows written.

    Reads recent non-archived sessions (returns ``0`` if none), clusters them
    into themed chapters, and replaces the previous *generated* chapters
    (user-renamed ``edited`` chapters are preserved by the store). Fully
    fault-tolerant: any failure is logged and swallowed so it can never break
    the Dream run it hangs off.

    ``on_event`` (matching dream's ``OnEventFn``) receives ``stage_start`` /
    ``llm_text`` / ``stage_end`` so the run shows up in the same dream-run audit
    / HUD stream.
    """

    def _emit(event_type: str, payload: dict[str, Any]) -> None:
        if on_event is not None:
            try:
                on_event(event_type, payload)
            except Exception:  # noqa: BLE001 — telemetry must never break the run
                logger.exception("book_chapters: on_event failed (%s)", event_type)

    written = 0
    _emit("stage_start", {"stage": _STAGE})
    try:
        sessions = recent_sessions()
        if not sessions:
            _emit("stage_end", {"stage": _STAGE, "written": 0})
            return 0

        chapters = cluster_chapters(sessions)
        _emit(
            "llm_text",
            {
                "stage": _STAGE,
                "text": f"clustered {len(chapters)} chapter(s) from {len(sessions)} session(s)",
            },
        )

        with fts.cursor() as conn:
            written = chapters_store.replace_generated(conn, chapters)
    except Exception:  # noqa: BLE001 — never propagate into the dream run
        logger.exception("book_chapters: run_book_chapters failed")

    _emit("stage_end", {"stage": _STAGE, "written": written})
    return written


# Built lazily so the daemon's already-loaded Config is preferred, but tests /
# one-off calls still work without threading a Config through every call site.
_CONFIG: Config | None = None


def _cfg() -> Config:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    return _CONFIG
