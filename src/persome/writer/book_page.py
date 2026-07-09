"""Book-page generation: turn a day's worth-remembering episodes into prose.

Two decoupled LLM steps, each its own prompt so they can be tested and tuned
independently:

1. :func:`select_episodes` — read the day's ``event-daily`` text and judge which
   moments (0–N, conservatively) are worth a page.
2. :func:`write_episode` — write one literary, second-person page for one
   selected episode.

This module owns the prompts/LLM calls only; persistence is
:mod:`persome.store.book_pages` and orchestration (Task 4) is
:func:`run_book_pages`, hung off the daily Dream run.

``call_llm`` is imported into module scope so tests can monkeypatch
``book_page.call_llm`` directly.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .. import paths
from ..config import Config
from ..config import load as load_config
from ..logger import get
from ..prompts import load as load_prompt
from ..store import book_pages
from .llm import OnEventFn, call_llm, extract_text

logger = get("persome.writer")

_SELECT_STAGE = "book_page"
_WRITE_STAGE = "book_page"

# Match the first top-level JSON array in a possibly-chatty LLM reply.
_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)

# Leading markdown ATX heading marker on a title line ("# ", "### ", …).
_HEADING_PREFIX_RE = re.compile(r"^#{1,6}\s*")
# Surrounding emphasis / quote / list noise the model sometimes wraps a title in.
_TITLE_WRAP_CHARS = "*_`\"'“”‘’ \t"


def _clean_title(raw: str) -> str:
    """Strip markdown noise off a title line so frontmatter ``title`` is clean.

    The writer prompt asks for a bare title line, but models intermittently
    emit ``# The voices you tried on``, ``**A Title**``, or a leading bullet.
    Strip a leading ATX heading marker (``#``–``######``) and surrounding
    emphasis / quote / list characters, leaving the human phrase. Idempotent and
    never raises — a fully-noise title degrades to ``""`` (caller falls back).
    """
    title = raw.strip()
    if not title:
        return ""
    title = title.lstrip("-•").strip()
    title = _HEADING_PREFIX_RE.sub("", title)
    return title.strip(_TITLE_WRAP_CHARS).strip()


def _extract_json_array(text: str) -> list[Any]:
    """Parse the first ``[...]`` array out of ``text``; ``[]`` on any failure.

    The model is asked for a bare JSON array but may wrap it in prose or a code
    fence — be robust: try the whole string first, then the first bracketed
    span. Never raise; a parse failure means "no episodes."
    """
    for candidate in (text, _first_array_span(text)):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, list):
            return parsed
    return []


def _first_array_span(text: str) -> str | None:
    m = _ARRAY_RE.search(text)
    return m.group(0) if m else None


def select_episodes(date: str, daily_text: str) -> list[dict[str, Any]]:
    """Select 0–N page-worthy episodes from a day's event-daily text.

    Conservative by design: a flat day returns ``[]``. Each returned dict has
    ``{"anchor": str, "source_refs": list[str]}``. Malformed items are dropped.
    """
    system = load_prompt("book_page_select.md")
    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": f"Date: {date}\n\n# Today's event-daily log\n\n{daily_text}",
        },
    ]
    cfg = _cfg()
    resp = call_llm(cfg, _SELECT_STAGE, messages=messages)
    text = extract_text(resp)

    episodes: list[dict[str, Any]] = []
    for item in _extract_json_array(text):
        if not isinstance(item, dict):
            continue
        anchor = item.get("anchor")
        if not isinstance(anchor, str) or not anchor.strip():
            continue
        refs = item.get("source_refs")
        source_refs = [str(r) for r in refs] if isinstance(refs, list) else []
        episodes.append({"anchor": anchor.strip(), "source_refs": source_refs})
    return episodes


def write_episode(date: str, episode: dict[str, Any], daily_text: str) -> dict[str, str]:
    """Write one literary, second-person page for a selected episode.

    Returns ``{"title": str, "body": str}``. The LLM is asked for a title on the
    first line, a blank line, then prose; we split on that. If the model returns
    nothing usable, the title falls back to the episode anchor and body is empty
    — the caller (run_book_pages) decides whether to keep an empty page.
    """
    system = load_prompt("book_page.md")
    anchor = str(episode.get("anchor") or "")
    refs = ", ".join(str(r) for r in (episode.get("source_refs") or []))
    user = (
        f"Date: {date}\n\n"
        f"# Episode to write\n\n{anchor}\n\n"
        f"# Source references\n\n{refs or '(none)'}\n\n"
        f"# Today's event-daily log (for grounding)\n\n{daily_text}"
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    resp = call_llm(_cfg(), _WRITE_STAGE, messages=messages)
    text = extract_text(resp).strip()
    return _split_title_body(text, fallback_title=anchor)


def _split_title_body(text: str, *, fallback_title: str) -> dict[str, str]:
    """Split LLM prose into ``{title, body}``: first non-blank line is the title,
    the remainder (after a blank line) is the body."""
    if not text.strip():
        return {"title": fallback_title, "body": ""}
    lines = text.splitlines()
    # First non-blank line = title.
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    raw_title = lines[idx] if idx < len(lines) else fallback_title
    title = _clean_title(raw_title)
    body = "\n".join(lines[idx + 1 :]).strip()
    return {"title": title or fallback_title, "body": body}


def _read_event_daily(date: str) -> str | None:
    """Return the day's ``event-<date>.md`` text, or ``None`` if it doesn't exist."""
    path = paths.memory_dir() / f"event-{date}.md"
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def run_book_pages(date: str, *, on_event: OnEventFn | None = None) -> list[str]:
    """Generate book pages for one day. Returns the list of written page ids.

    Reads the day's event-daily (returns ``[]`` if absent), selects episodes
    (conservatively, possibly none), writes one page per episode. The whole run
    is fault-tolerant: any per-episode failure is logged and skipped, and any
    top-level failure returns whatever was written so far — a book-page failure
    must never break the Dream run it hangs off.

    ``on_event`` (matching dream's ``OnEventFn``) receives ``stage_start`` /
    ``llm_text`` / ``stage_end`` so the run shows up in the same dream-run audit
    / HUD stream.
    """

    def _emit(event_type: str, payload: dict[str, Any]) -> None:
        if on_event is not None:
            try:
                on_event(event_type, payload)
            except Exception:  # noqa: BLE001 — telemetry must never break the run
                logger.exception("book_page: on_event failed (%s)", event_type)

    written: list[str] = []
    _emit("stage_start", {"stage": "book_page", "date": date})
    try:
        daily_text = _read_event_daily(date)
        if not daily_text:
            _emit("stage_end", {"stage": "book_page", "date": date, "written": 0})
            return []

        episodes = select_episodes(date, daily_text)
        _emit(
            "llm_text",
            {"stage": "book_page", "text": f"selected {len(episodes)} episode(s) for {date}"},
        )

        for episode in episodes:
            try:
                page = write_episode(date, episode, daily_text)
                if not page.get("title") and not page.get("body"):
                    continue  # nothing usable — skip rather than store an empty page
                pid = book_pages.write_page(
                    date=date,
                    title=page["title"],
                    body=page["body"],
                    source_refs=episode.get("source_refs", []),
                )
                written.append(pid)
                _emit("llm_text", {"stage": "book_page", "text": f"wrote {pid}: {page['title']}"})
            except Exception:  # noqa: BLE001 — one bad episode must not abort the rest
                logger.exception("book_page: write_episode failed for %s on %s", episode, date)
    except Exception:  # noqa: BLE001 — never propagate into the dream run
        logger.exception("book_page: run_book_pages failed for %s", date)

    _emit("stage_end", {"stage": "book_page", "date": date, "written": len(written)})
    return written


# Built lazily so the daemon's already-loaded Config is preferred, but tests /
# one-off calls still work without threading a Config through every call site.
_CONFIG: Config | None = None


def _cfg() -> Config:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    return _CONFIG
