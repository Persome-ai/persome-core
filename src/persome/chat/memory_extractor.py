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
        prompt = _render_prompt(conversation)

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


def _render_prompt(conversation: str) -> str:
    """Fill the prompt's conversation placeholder.

    Uses targeted ``str.replace`` rather than ``str.format`` because the prompt
    embeds a literal JSON example whose ``{ }`` braces would make ``format``
    raise.
    """
    return _load_prompt().replace("{conversation}", conversation)


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
