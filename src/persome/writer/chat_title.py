"""Generate a short sidebar title for a chat session.

Called once per session, after the first user/assistant exchange. Uses the
same Anthropic SDK path the chat agent uses (``chat.agent.complete_sync``
+ ``[chat] model`` + ``ANTHROPIC_*`` env), so any user who can chat at all
has a working title generator without configuring a separate ``[models.*]``
stage — title generation rides the same already-validated provider as their
chat replies.
"""

from __future__ import annotations

from typing import Any

from ..chat.agent import complete_sync
from ..config import Config
from ..logger import get

logger = get("persome.writer")

TITLE_MAX_CHARS = 24

_PROMPT_PREFIX = (
    "You name chat conversations for a sidebar list."
    " Given the first user message and assistant reply below, produce a"
    f" concise title in the user's language, ≤{TITLE_MAX_CHARS} characters."
    " No quotes, no trailing punctuation, no leading verbs like '关于' / 'About'."
    " Return ONLY the title text — no preamble, no explanation.\n\n"
)


def _content_to_text(content: Any) -> str:
    """Collapse stored Anthropic-shape content into a plain string for the prompt."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text" and isinstance(b.get("text"), str):
                parts.append(b["text"])
            # thinking blocks are private reasoning — drop them.
        return "".join(parts)
    return str(content)


def _first_user_and_assistant(messages: list[dict[str, Any]]) -> tuple[str, str]:
    """Return (first_user_text, first_assistant_text) — either may be ''.

    Walks messages in order; ignores synthetic user-role tool_result messages
    (content lists whose blocks are all non-text).
    """
    user_text = ""
    asst_text = ""
    for m in messages:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _content_to_text(m.get("content")).strip()
        if not text:
            continue
        if role == "user" and not user_text:
            user_text = text
        elif role == "assistant" and not asst_text:
            asst_text = text
        if user_text and asst_text:
            break
    return user_text, asst_text


def _clean(raw: str) -> str:
    """Normalize the LLM output into a one-line ≤TITLE_MAX_CHARS title."""
    s = raw.strip()
    # Strip surrounding quotes (single, double, Chinese, backticks) the model
    # often adds despite the instruction.
    while s and s[0] in "\"'`「『《【“‘":
        s = s[1:]
    while s and s[-1] in "\"'`」』》】”’":
        s = s[:-1]
    s = s.strip()
    s = " ".join(s.split())
    if len(s) > TITLE_MAX_CHARS:
        s = s[:TITLE_MAX_CHARS].rstrip() + "…"
    return s


def generate_title(cfg: Config, messages: list[dict[str, Any]]) -> str | None:
    """Generate a short title from the first user/assistant exchange.

    Returns None on any failure (empty input, LLM error, empty output) so the
    caller can keep the existing preview/timestamp fallback. Never raises.
    """
    user_text, asst_text = _first_user_and_assistant(messages)
    if not user_text:
        return None

    user_excerpt = user_text[:600]
    asst_excerpt = asst_text[:600] if asst_text else ""

    body = f"User: {user_excerpt}"
    if asst_excerpt:
        body += f"\n\nAssistant: {asst_excerpt}"
    body += "\n\nTitle:"

    try:
        # max_tokens budget must cover a reasoning-model "thinking" block AND
        # the actual title text. DeepSeek's /anthropic gateway routes
        # `deepseek-v4-flash` (and v4-pro) as reasoning models that emit a
        # thinking block before the text block — a tight budget (e.g. 64)
        # gets consumed entirely by thinking, the response stops with
        # `stop_reason: max_tokens` and no text block at all, and
        # ``complete_sync`` returns "". 1024 is comfortably above observed
        # thinking-block sizes for this prompt; the actual title is ≤24
        # chars so the cost is bounded by the thinking length, not by us.
        raw = complete_sync(
            cfg.chat,
            [{"role": "user", "content": _PROMPT_PREFIX + body}],
            max_tokens=1024,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("chat_title generation failed: %s", exc)
        return None

    cleaned = _clean(raw)
    return cleaned or None
