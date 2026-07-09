"""``view_capture`` — the lazy multimodal "look at the screenshot" tool (spec E5 / TODO #8).

Recognition is text-first and cheap; full image understanding is expensive, so the
daemon never describes screenshots eagerly. This tool is the **on-demand** other half:
when an agent wants to actually *see* the screen behind an intent, it calls
``view_capture(intent_id=...)``, the daemon locates the most relevant capture(s),
decrypts the screenshot, asks an injected VLM seam a **targeted** question, and writes
the answer back as a durable evomem text memory (self-heal: from then on it's text,
no second VLM call).

Design points:

- **VLM is an injected seam** (:data:`vlm_describe`). The daemon ships no local VLM
  today, so the default seam (:func:`_stub_vlm`) gracefully returns a "not configured"
  marker; real wiring is left for whoever lands a VLM backend. Tests inject a fake.
- **Screenshot content is UNTRUSTED data.** Anything the VLM reads off the screen is
  observed data, never an instruction. :func:`sanitize_observed` strips control chars
  and neutralises fence/injection markers before the text is persisted or returned.
- **Write-back rides the public evomem entrance** (``EvoMemory.add_direct`` →
  ``L5_KNOWLEDGE``, no LLM), keyed by ``file_name`` so the memory links back to the
  intent/capture it came from.
- **Off by default** — gated on ``cfg.view_capture_enabled`` (a field the config owner
  wires in separately; we only read it via ``getattr(..., False)``).

This module only *reads* from ``capture/screenshot_crypto``, ``timeline/aggregator``
and ``intent/store``, and *writes* solely through ``evomem.engine.EvoMemory``.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..capture import screenshot_crypto
from ..evomem.engine import EvoMemory
from ..evomem.models import MemoryLayer
from ..intent import store as intent_store
from ..intent.ontology import Intent
from ..logger import get
from ..store import fts
from ..timeline import aggregator

logger = get("persome.mcp")

#: A VLM seam: ``(image_bytes, question) -> answer_text``. The default is a stub
#: because the daemon currently has no bundled vision model — real wiring (a local
#: VLM, or a metered backend route mirroring the Anthropic proxy) is left to whoever
#: lands a vision backend. Tests inject a fake. Override the module attribute to wire
#: a real one: ``view_capture.vlm_describe = my_impl``.
VlmDescribe = Callable[[bytes, str], str]

#: How wide a window around the intent's recognition time to search for its capture
#: when no ``source="capture"`` evidence pins an exact stem. The scheduler captures
#: roughly once a minute, so a couple of minutes either side bounds it to the few
#: frames around the moment the intent was recognised.
_WINDOW = timedelta(minutes=2)

#: Hard cap on how many captures we ever feed to the VLM — "the 1-2 most relevant".
_MAX_CAPTURES = 2

_DISABLED_MSG = "view_capture is disabled (set view_capture_enabled to use it)."
_VLM_UNCONFIGURED_MSG = "VLM 未配置"  # stub answer when no real seam is wired


def _stub_vlm(image_bytes: bytes, question: str) -> str:  # noqa: ARG001
    """Default VLM seam — no model is bundled, so degrade gracefully (never raise)."""
    return _VLM_UNCONFIGURED_MSG


#: Module-level seam, monkeypatchable in tests / at wiring time.
vlm_describe: VlmDescribe = _stub_vlm


# --------------------------------------------------------------------------- #
# Untrusted-content sanitization
# --------------------------------------------------------------------------- #

#: Fence / instruction markers an attacker might paint on screen to break out of the
#: untrusted-data block and inject a trusted instruction. We neutralise them by
#: inserting a zero-width space so the literal text is preserved (still human-readable
#: in the memory) but the marker can no longer act as a real delimiter / directive.
_FENCE_MARKERS = (
    "```",
    "<observed_screen_text",
    "</observed_screen_text",
    "<untrusted",
    "</untrusted",
    "[/INST]",
    "[INST]",
    # ChatML (OpenAI / Qwen)
    "<|im_start|>",
    "<|im_end|>",
    "<|system|>",
    "<|user|>",
    "<|assistant|>",
    # Llama 2/3 + Mistral turn/role tokens
    "<|eot_id|>",
    "<|start_header_id|>",
    "<|end_header_id|>",
    "</s>",
    "<s>",
    # Gemma turn tokens
    "<start_of_turn>",
    "<end_of_turn>",
    # Alpaca / instruction-tuned section headers
    "### instruction:",
    "### system:",
    "### response:",
    # generic role tags
    "<system>",
    "</system>",
)

# One case-insensitive alternation over all markers, longest-first so a longer marker
# (e.g. ``<|im_start|>``) wins over a prefix. Matching is case-insensitive so a
# look-alike like ``[InsT]`` or ``### Instruction:`` is still caught.
_FENCE_RE = re.compile(
    "|".join(re.escape(m) for m in sorted(_FENCE_MARKERS, key=len, reverse=True)),
    re.IGNORECASE,
)

_ZWSP = "​"  # zero-width space, defangs a marker without dropping its text


def sanitize_observed(text: str) -> str:
    """Neutralise untrusted screen-derived text before it is trusted as data.

    - drops control characters (except ``\\n`` / ``\\t``) so escape sequences /
      terminal control bytes can't smuggle anything through,
    - normalises to NFKC so fullwidth/look-alike fence chars fold to the markers
      we then defang,
    - inserts a zero-width space into known fence / instruction / chat-template markers
      (case-insensitively) so a forged closing fence, ``[INST]``, ``<|im_start|>``,
      ``<end_of_turn>`` or ``### Instruction:`` can't break out of the untrusted block.

    The result is still legible (markers stay visible, original case preserved) but inert.
    This is a local minimal implementation — there is no shared sanitizer in the Python
    daemon yet.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    # Strip control chars (category Cc) except newline + tab; keep printable + CJK.
    cleaned = "".join(ch for ch in text if ch in ("\n", "\t") or unicodedata.category(ch) != "Cc")
    # Splice a ZWSP after the first char of each matched marker so the token no longer
    # matches a parser, preserving the original (matched) casing for legibility.
    return _FENCE_RE.sub(lambda m: m.group(0)[0] + _ZWSP + m.group(0)[1:], cleaned)


# --------------------------------------------------------------------------- #
# Locating the intent + its capture(s)
# --------------------------------------------------------------------------- #


def _get_intent(intent_id: int) -> Intent | None:
    with fts.cursor() as conn:
        intent_store.ensure_schema(conn)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            f"SELECT {intent_store._SELECT_COLS} FROM intents WHERE id = ?",
            (intent_id,),
        ).fetchone()
    return intent_store._row_to_intent(row) if row else None


def _parse_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    # Naive ts (an offset-less recognition time) → assume local tz, then it still
    # compares fine against the window we build from the same parsed value.
    return dt


def _capture_paths_for_intent(intent: Intent) -> list[Path]:
    """Find the most relevant capture file(s) backing this intent.

    Preference order:
      1. ``evidence[*].source == "capture"`` → the exact capture stem (``<root>/
         capture-buffer/<stem>.json``) the recognizer cited. Cheapest + most precise.
      2. Otherwise a ``±_WINDOW`` time window around ``intent.ts`` via
         :func:`aggregator.captures_in_window`, picking the captures nearest the ts.

    Returns at most :data:`_MAX_CAPTURES`, nearest-first.
    """
    buf = aggregator_capture_buffer_dir()

    # 1) Exact capture stems cited as evidence.
    cited: list[Path] = []
    for ev in intent.evidence:
        if ev.source == "capture" and ev.ref_id:
            p = buf / f"{ev.ref_id}.json"
            if p.is_file() and p not in cited:
                cited.append(p)
    if cited:
        return cited[:_MAX_CAPTURES]

    # 2) Time window around the recognition ts.
    ts = _parse_ts(intent.ts)
    if ts is None:
        return []
    files = aggregator.captures_in_window(ts - _WINDOW, ts + _WINDOW)
    if not files:
        return []

    def _dist(p: Path) -> float:
        # Reuse the aggregator's stem→datetime decoder (it reverses scheduler.py's
        # ``:``→``-`` / ``+``→``p`` sanitisation), so ranking matches the window.
        stem_ts = aggregator._stem_to_dt(p.stem)
        if stem_ts is None:
            return float("inf")
        try:
            return abs((stem_ts - ts).total_seconds())
        except TypeError:  # mixed naive/aware — normalise both to UTC
            a = stem_ts.replace(tzinfo=UTC)
            b = ts.replace(tzinfo=UTC)
            return abs((a - b).total_seconds())

    return sorted(files, key=_dist)[:_MAX_CAPTURES]


def aggregator_capture_buffer_dir() -> Path:
    """Indirection so tests don't have to reach into ``aggregator`` internals."""
    from .. import paths

    return paths.capture_buffer_dir()


def _load_capture_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("view_capture: failed to load capture %s: %s", path.name, exc)
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------- #
# The tool
# --------------------------------------------------------------------------- #


def view_capture(
    *,
    intent_id: int,
    question: str,
    cfg: object,
    evo: EvoMemory | None = None,
) -> str:
    """Look at the screenshot behind ``intent_id`` and answer ``question``.

    Flow: locate the intent's capture(s) → decrypt the screenshot bytes →
    ask the (injected) VLM seam a targeted question → sanitize the untrusted answer
    → write it back as an ``L5_KNOWLEDGE`` evomem text memory → return the answer.

    - Gated on ``cfg.view_capture_enabled`` (default off) → returns the disabled
      notice and writes nothing.
    - Fail-soft throughout: a missing intent / capture / unreadable screenshot / VLM
      error all return a short human-readable status rather than raising.
    - The answer is treated as untrusted observed data (sanitised) before it is
      persisted or returned.

    Returns the (sanitised) answer text, or a status string.
    """
    if not getattr(cfg, "view_capture_enabled", False):
        return _DISABLED_MSG

    question = (question or "").strip() or "What is shown on screen?"

    intent = _get_intent(intent_id)
    if intent is None:
        return f"view_capture: no intent with id {intent_id}."

    capture_paths = _capture_paths_for_intent(intent)
    if not capture_paths:
        return f"view_capture: no capture found for intent {intent_id}."

    image_bytes: bytes | None = None
    used_path: Path | None = None
    for path in capture_paths:
        data = _load_capture_json(path)
        if data is None:
            continue
        decoded = screenshot_crypto.read_screenshot(data)
        if decoded:
            image_bytes = decoded
            used_path = path
            break

    if image_bytes is None or used_path is None:
        return (
            f"view_capture: capture for intent {intent_id} has no readable "
            "screenshot (stripped or undecryptable)."
        )

    try:
        raw_answer = vlm_describe(image_bytes, question)
    except Exception as exc:  # noqa: BLE001 — a flaky VLM must never crash the tool
        logger.warning("view_capture: VLM seam raised: %s", exc)
        return "view_capture: VLM error (see logs)."

    answer = sanitize_observed(raw_answer or "")
    if not answer or answer == _VLM_UNCONFIGURED_MSG:
        # Empty or the stub's "not configured" marker → nothing worth persisting;
        # degrade gracefully (no evomem write) and surface the marker.
        return _VLM_UNCONFIGURED_MSG

    # Write the seen text back through the public evomem entrance. The content is
    # framed as observed (untrusted) data; file_name links it to the source capture
    # so the memory is traceable back to the intent/screenshot it describes. The
    # ``topic-`` prefix is required by the evomem write口 (VALID_PREFIXES) and is the
    # apt bucket for "knowledge distilled from observed screen content".
    file_name = f"topic-view-capture-{used_path.stem}.md"
    body = (
        f"[observed screen, intent #{intent_id}, capture {used_path.stem}] "
        f"Q: {sanitize_observed(question)}\nA: {answer}"
    )
    try:
        evo = evo or EvoMemory()
        evo.add_direct(body, layer=MemoryLayer.L5_KNOWLEDGE, file_name=file_name)
    except Exception as exc:  # noqa: BLE001 — write-back failure shouldn't lose the answer
        logger.warning("view_capture: evomem write-back failed: %s", exc)

    return answer


def register(server: object, cfg: object) -> None:
    """Register ``view_capture`` as an MCP tool on ``server`` (FastMCP).

    Kept thin: the FastMCP ``@server.tool()`` wrapper exposes a stable signature and
    delegates to :func:`view_capture`, capturing ``cfg`` so the enable gate is read
    per call. Off by default, so when ``view_capture_enabled`` is false the tool is
    still listed but no-ops with the disabled notice.
    """

    @server.tool()  # type: ignore[attr-defined]
    def view_capture_tool(intent_id: int, question: str) -> str:
        """Look at the screenshot behind a recognized intent and answer a targeted question.

        Use SPARINGLY — this is the expensive multimodal path. Most context questions
        are answered by the text tools (``search``, ``search_captures``,
        ``recent_activity``). Reach for this only when you must actually *see* the
        screen behind an intent (a chart, a diagram, an image with no extractable
        text). Pass a SPECIFIC ``question`` (e.g. "What's the error in the red
        toast?"), not "describe everything". The answer is also saved as a memory so
        a follow-up is answered from text without re-looking.

        Disabled by default; returns a notice when off.
        """
        return view_capture(intent_id=intent_id, question=question, cfg=cfg)
