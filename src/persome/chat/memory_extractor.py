"""Background memory extraction from chat conversations.

写权反转（PR-6b，SSOT 切换设计 §1.3/§5）：``write_authority="evomem"`` 时本站点
的记忆写（``_write_memory`` 的 create+append）经 ``store/entries.py`` 的
choke-point dispatch 走 evomem engine 落 evo_nodes，markdown 由投影器再生成；
``_content_already_present`` 的 dedup 守卫读的是投影文件（写后同步刷新）。
抽取决策仍由本站点的 LLM 完成——经 engine reconcile 调和（``add``）的语义升级
与写权反转解耦，留待后续显式启用。逐站输出等价由
``tests/test_evomem/test_inversion_stations.py`` 钉死。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable
from typing import Any

from .. import config as config_mod
from ..logger import get as _get_logger
from ..store import entries as entries_mod
from ..store import files as files_mod
from ..store import fts
from .agent import complete_sync

_logger = _get_logger("persome.chat")

# Extraction throttle: only run every N turns or after token growth
_MIN_TURNS_BETWEEN_EXTRACTIONS = 3
_MIN_TOKEN_GROWTH_FOR_EXTRACTION = 5_000

# ─── Known-memory priming (E4) ───────────────────────────────────────────────
# When `cfg.extraction_known_memory_priming` is on, the extractor is fed a
# cached summary of what we already know so it produces deltas/updates rather
# than restating existing facts. The summary is wrapped with an anti-anchoring
# guard: the live conversation always wins on conflict, and the summary is a
# hint only — never license to invent facts not present in the conversation.

# Provider seam: returns a plain-text "known memory" summary string. Defaults to
# returning "" (no priming material). Tests inject a fake that returns canned
# text and counts calls; the daemon can later wire a live recent-memory reader
# (e.g. fts.recent) without touching the extraction logic.
KnownMemoryProvider = Callable[[], str]


def _default_known_memory_provider() -> str:
    return ""


_known_memory_provider: KnownMemoryProvider = _default_known_memory_provider

# Small TTL cache so we don't rebuild the summary on every extraction in a
# burst — mirrors the few-minute cache habit elsewhere in the daemon.
_KNOWN_MEMORY_TTL_SECONDS = 300.0
_known_memory_cache: tuple[float, str] | None = None
_known_memory_lock = threading.Lock()

_ANTI_ANCHORING = (
    "When the known summary below conflicts with the current conversation, "
    "trust the current observation — the summary is only a hint, and you must "
    "not invent facts that do not appear in the current conversation. Prefer "
    "emitting updates/deltas over restating facts the summary already covers."
)


def set_known_memory_provider(provider: KnownMemoryProvider | None) -> None:
    """Inject the known-memory summary source (test/seam hook).

    Passing None restores the default empty provider. Also clears the cache so a
    freshly-injected provider isn't shadowed by a stale entry.
    """
    global _known_memory_provider, _known_memory_cache
    with _known_memory_lock:
        _known_memory_provider = provider or _default_known_memory_provider
        _known_memory_cache = None


def _cached_known_memory(*, now: float | None = None) -> str:
    """Return the known-memory summary, rebuilt at most once per TTL window."""
    global _known_memory_cache
    ts = time.monotonic() if now is None else now
    with _known_memory_lock:
        cached = _known_memory_cache
        if cached is not None and (ts - cached[0]) < _KNOWN_MEMORY_TTL_SECONDS:
            return cached[1]
        provider = _known_memory_provider
    # Build outside the lock (provider may do IO); tolerate failures.
    try:
        summary = (provider() or "").strip()
    except Exception:
        _logger.debug("known-memory provider failed", exc_info=True)
        summary = ""
    with _known_memory_lock:
        _known_memory_cache = (ts, summary)
    return summary


def _build_known_memory_block(cfg: config_mod.Config) -> str:
    """The prompt block injected before the conversation.

    Empty string when the feature is off (so the prompt is byte-identical to the
    pre-feature behavior) or when there is no known-memory material to inject.
    """
    if not getattr(cfg, "extraction_known_memory_priming", False):
        return ""
    summary = _cached_known_memory()
    if not summary:
        return ""
    return f"Known memory summary (cached; may be stale):\n{_ANTI_ANCHORING}\n\n{summary}\n\n"


# The extractor's free taxonomy (user/feedback/project/reference) is mapped onto
# the canonical memory-file prefixes the classifier writes, so chat-learned facts
# converge into the SAME structured store + FTS rather than living as orphan
# markdown the rest of the pipeline can't see. feedback→user (it's about how to
# treat this user); reference→topic (external resources are topical). user/project
# map straight through.
_TYPE_TO_PREFIX = {
    "user": "user",
    "feedback": "user",
    "project": "project",
    "reference": "topic",
}


def maybe_extract(
    messages: list[dict[str, Any]],
    cfg: config_mod.Config,
    *,
    last_extracted_index: int = 0,
    tokens_at_last_extraction: int = 0,
) -> tuple[int, int]:
    """Check if extraction should run and execute it in a background thread.

    Returns updated ``(last_extracted_index, tokens_at_last_extraction)``.
    """
    new_messages = messages[last_extracted_index:]
    if not new_messages:
        return last_extracted_index, tokens_at_last_extraction

    # Count model-visible messages (user + assistant) since last extraction
    visible_count = sum(1 for m in new_messages if m.get("role") in ("user", "assistant"))

    current_tokens = _rough_token_estimate(messages)
    token_growth = current_tokens - tokens_at_last_extraction

    should_run = (
        visible_count >= _MIN_TURNS_BETWEEN_EXTRACTIONS * 2
        or token_growth >= _MIN_TOKEN_GROWTH_FOR_EXTRACTION
    )

    if not should_run:
        return last_extracted_index, tokens_at_last_extraction

    # Fire-and-forget background extraction
    thread = threading.Thread(
        target=_run_extraction,
        args=(new_messages, cfg),
        daemon=True,
    )
    thread.start()

    return len(messages), current_tokens


def _rough_token_estimate(messages: list[dict[str, Any]]) -> int:
    total = 0
    for m in messages:
        content = m.get("content") or ""
        total += len(content) // 4
        for tc in m.get("tool_calls", []):
            fn = tc.get("function", {})
            total += len(fn.get("arguments", "")) // 4
    return total


def _run_extraction(
    new_messages: list[dict[str, Any]],
    cfg: config_mod.Config,
) -> None:
    """Execute memory extraction and write results to disk."""
    try:
        conversation = _format_for_extraction(new_messages)
        prompt = _render_prompt(conversation, _build_known_memory_block(cfg))

        raw = complete_sync(
            cfg.chat,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2048,
        ).strip()
        if not raw:
            return

        memories = json.loads(raw)
        if not isinstance(memories, list):
            return

        with fts.cursor() as conn:
            for mem in memories:
                _write_memory(conn, mem)
    except Exception:
        _logger.debug("memory extraction failed", exc_info=True)


def _format_for_extraction(messages: list[dict[str, Any]]) -> str:
    from .handler import _strip_time_prefix

    lines: list[str] = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content") or ""
        if role == "user":
            lines.append(f"User: {_strip_time_prefix(content)}")
        elif role == "assistant":
            if content:
                lines.append(f"Assistant: {content}")
            for tc in m.get("tool_calls", []):
                fn = tc.get("function", {})
                lines.append(f"  [called {fn.get('name', '?')}]")
        elif role == "tool":
            preview = content[:300] + "..." if len(content) > 300 else content
            lines.append(f"  [tool result: {preview}]")
    return "\n".join(lines)


def _load_prompt() -> str:
    from ..prompts import load as load_prompt

    return load_prompt("chat_memory_extract.md")


def _render_prompt(conversation: str, known_memory_block: str) -> str:
    """Fill the prompt's two placeholders.

    Uses targeted ``str.replace`` rather than ``str.format`` because the prompt
    embeds a literal JSON example whose ``{ }`` braces would make ``format``
    raise. ``{known_memory}`` collapses to "" when priming is off, so the
    rendered prompt is byte-identical to the pre-feature output in that case.
    """
    return (
        _load_prompt()
        .replace("{known_memory}", known_memory_block)
        .replace("{conversation}", conversation)
    )


def _resolve_name(mem_type: str, name: str) -> str:
    """Map an extracted memory to a canonical, validly-prefixed file slug.

    Chat used to write ``{type}-{name}.md`` raw — which both double-prefixed the
    common case (``name='user-preferences'`` → ``user-user-preferences``) and
    produced invalid prefixes (``feedback-`` / ``reference-`` aren't in
    ``VALID_PREFIXES``), so those files never entered the index. Here we keep an
    already-valid slug as-is and otherwise prepend the mapped prefix.
    """
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name).lower().strip("-")
    if any(safe.startswith(p) for p in files_mod.VALID_PREFIXES):
        return safe
    prefix = _TYPE_TO_PREFIX.get(mem_type, "topic")
    return f"{prefix}-{safe}"


def _write_memory(conn: sqlite3.Connection, mem: dict[str, Any]) -> None:
    """Persist one extracted memory through the unified store (entries + FTS).

    Same write path the classifier uses, so chat-learned facts become first-class,
    searchable memory instead of orphan markdown. Create-or-append, with a content
    dedup guard so re-extracting the same fact across turns doesn't grow the file.
    """
    mem_type = str(mem.get("type", "reference"))
    name = str(mem.get("name", "")).strip()
    description = str(mem.get("description", "")).strip()
    content = str(mem.get("content", "")).strip()

    if not name or not content:
        return

    file_name = _resolve_name(mem_type, name)
    tags = [mem_type]

    path = files_mod.memory_path(file_name)
    if not path.exists():
        entries_mod.create_file(
            conn, name=file_name, description=description or content[:80], tags=tags
        )
    elif _content_already_present(path, content):
        return

    entries_mod.append_entry(conn, name=file_name, content=content, tags=tags)


def _content_already_present(path: Any, content: str) -> bool:
    """True if an existing entry already holds this exact content (dedup guard)."""
    try:
        parsed = files_mod.read_file(path)
    except Exception:
        return False
    target = content.strip()
    return any((e.body or "").strip() == target for e in parsed.entries)
