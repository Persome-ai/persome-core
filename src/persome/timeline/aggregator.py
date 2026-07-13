"""Build one TimelineBlock from a short (default 1-minute) window of captures.

Reads capture-buffer JSON files whose ``timestamp`` falls inside the
window, renders them into a prompt, and asks the LLM to produce a
small list of self-contained ``[App] …`` lines. Idempotent: skips
windows that already have a block.

The prompt reads the structured S1 fields (``focused_element``,
``visible_text``, ``url``) written by ``capture/s1_parser.py`` rather
than re-rendering the raw AX tree. Pre-v2 captures without those
fields are back-rendered via ``ax_tree_to_markdown`` as a fallback.
"""

from __future__ import annotations

import json
from datetime import datetime, tzinfo
from pathlib import Path

from .. import paths
from ..capture import s1_parser
from ..capture.ax_models import ax_tree_to_markdown
from ..capture.timestamps import parse_capture_path_timestamp
from ..config import Config
from ..logger import get
from ..parsers import parser_for_capture
from ..prompts import load as load_prompt
from ..store import entries as entries_mod
from ..store import fts as fts_store
from ..store import parser_ticks
from ..writer import llm as llm_mod
from .attention_locus import AttentionLocus, resolve_locus
from .attention_locus import click_anchor as _click_anchor
from .attention_locus import focus_pane as _focus_pane

# Re-exported for the flag-off path + existing unit tests (test_click_anchor /
# test_focus_pane import these names from this module). The implementations now
# live in attention_locus.py so the resolver and the legacy feed share one copy.
__all__ = ["_click_anchor", "_focus_pane"]
from . import store

logger = get("persome.timeline")

# Per-capture slice that goes into the timeline prompt. S1 parser
# already caps visible_text at 10k; the timeline prompt is now a
# verbatim-preserving normalizer, so we want to keep as much as the
# context budget allows. 1-min windows rarely carry more than ~6
# captures in practice.
_PER_CAPTURE_TEXT_LIMIT = 4000
# Defensive ceiling: if something goes haywire and a 1-min window has
# 30+ captures, keep the newest ones. Later events are more recent and
# tend to be more informative.
_MAX_EVENTS_PER_WINDOW = 30

# Terminal emulators that scroll oldest-to-newest: the most recent content
# is at the bottom, so tail-truncation gives better intent signal than
# head-truncation.
#
# cmux (com.cmuxterm.app) is GPU-rendered and exposes ~no AX text, so the
# capture worker injects the real terminal surface (read over cmux's RPC)
# at the *end* of visible_text, after the AX chrome (workspace/tab sidebar,
# split buttons, update banner). Tail-truncation therefore keeps the injected
# terminal content — the actual attention target — and drops the leading
# chrome. Without this, the default head-slice keeps the chrome and cuts the
# terminal content exactly when a session is busy enough to overflow the cap.
_TERMINAL_BUNDLES: frozenset[str] = frozenset(
    {
        "com.googlecode.iterm2",
        "com.apple.Terminal",
        "io.alacritty",
        "net.kovidgoyal.kitty",
        "com.cmuxterm.app",
    }
)

# Chat/IM apps where message threads scroll downward (newest at bottom).
# The AX tree exposes full message content for these Electron-based apps,
# so tail-truncation surfaces the most recent messages instead of the oldest.
_CHAT_BUNDLES: frozenset[str] = frozenset(
    {
        "com.electron.lark",  # Feishu / Lark
    }
)


def _slice_visible_text(
    visible_text: str,
    focused_value: str,
    bundle: str,
    limit: int,
) -> str:
    """Return at most *limit* chars of *visible_text*, prioritising the region
    around the focused element's typed content.

    Strategy (tried in order):

    1. **Focused-value anchor** — if the focused element has a meaningful
       value (>20 chars), locate its opening in *visible_text* and return a
       context window centred slightly behind the match (25 % pre, 75 % post).
       This works for any app: text editors, chat composers, terminal inputs.
    2. **Terminal / chat tail** — for terminal emulators and Electron chat apps
       (e.g. Feishu), return the trailing *limit* chars.  Both scroll
       oldest-to-newest, so leading content is stale scrollback / old messages.
    3. **Default head** — for everything else, return the leading *limit*
       chars (page title + main body for browsers / document viewers).
    """
    if len(visible_text) <= limit:
        return visible_text

    search = (focused_value or "").strip()
    if len(search) > 20:
        idx = visible_text.find(search[:80])
        if idx >= 0:
            pre = limit // 2
            start = max(0, idx - pre)
            end = min(len(visible_text), start + limit)
            prefix = "…\n" if start > 0 else ""
            suffix = "\n…" if end < len(visible_text) else ""
            return prefix + visible_text[start:end] + suffix

    if bundle in _TERMINAL_BUNDLES | _CHAT_BUNDLES:
        return "…\n" + visible_text[-limit:]

    return visible_text[:limit] + "\n…"


# Budget for the raw focus excerpt stored on each block (a lossless backstop for
# session modeling; see TimelineBlock.focus_excerpt). Generous head slice so a
# chat message that sits just after the sidebar (the common AX layout) is always
# included — the chat-"tail" heuristic in _slice_visible_text would miss it.
_FOCUS_EXCERPT_CHARS = 8000


def _focus_excerpt(parsed: list[tuple[Path, dict]]) -> str:
    for _p, data in reversed(parsed):
        vt = data.get("visible_text")
        if vt is None:
            ax = data.get("ax_tree")
            vt = ax_tree_to_markdown(ax) if ax else ""
        vt = str(vt).strip()
        if vt:
            return vt[:_FOCUS_EXCERPT_CHARS]
    return ""


def _focus_structured_with_outcome(
    parsed: list[tuple[Path, dict]],
) -> tuple[str, str | None, str | None, str | None]:
    """Per-app structured conversation + telemetry outcome for the window.

    Walks newest→oldest. For the first capture whose app (``window_meta.bundle_id``)
    has a registered parser, that parser decides the outcome:

    - parser raised → ``("", bundle, "miss", "exception")``
    - ``parse`` → ``None`` → ``("", bundle, "miss", "decline")``
    - ``render()`` empty → ``("", bundle, "miss", "empty_render")``
    - ``render()`` non-empty → ``(rendered, bundle, "hit", None)``

    If **no** capture's app had a parser but at least one capture carried an
    ``ax_tree`` + ``bundle`` → ``("", <most-recent-such-bundle>, "fallback", None)``
    (session modeling then falls back to the raw ``focus_excerpt``).

    If the window had nothing parseable at all (no ax_tree+bundle) →
    ``("", None, None, None)`` and the caller records no tick.

    Every miss additionally emits one structured ``parser_miss`` log line
    (``bundle= reason= capture=``) so the three causes are separable in the
    logs without touching the ``parser_ticks`` schema (#548).

    Never raises — a parser failure on one capture is logged and treated as a
    ``miss``. Returns ``(text, bundle, outcome, miss_reason)``.
    """
    fallback_bundle: str | None = None
    for _p, data in reversed(parsed):
        ax = data.get("ax_tree")
        if not ax:
            continue
        wm = data.get("window_meta") or {}
        bundle = str(wm.get("bundle_id") or "")
        parser = parser_for_capture(bundle, ax if isinstance(ax, dict) else None)
        if parser is None:
            # Remember the newest ax_tree-bearing bundle so a window with no
            # parseable app is still attributed to a real app on fallback.
            if fallback_bundle is None:
                fallback_bundle = bundle
            continue
        try:
            conv = parser.parse(ax, window_title=wm.get("title"))
        except Exception as exc:  # noqa: BLE001 - a parser bug must not break the tick
            logger.warning(
                "timeline: parser_miss bundle=%s reason=exception capture=%s error=%s",
                bundle,
                _p.name,
                exc,
            )
            return "", bundle, "miss", "exception"
        if conv is None:
            logger.info(
                "timeline: parser_miss bundle=%s reason=decline capture=%s", bundle, _p.name
            )
            return "", bundle, "miss", "decline"
        rendered = conv.render().strip()
        if not rendered:
            logger.info(
                "timeline: parser_miss bundle=%s reason=empty_render capture=%s", bundle, _p.name
            )
            return "", bundle, "miss", "empty_render"
        return rendered, bundle, "hit", None
    if fallback_bundle is not None:
        return "", fallback_bundle, "fallback", None
    return "", None, None, None


def captures_in_window(start: datetime, end: datetime) -> list[Path]:
    buf = paths.capture_buffer_dir()
    if not buf.exists():
        return []
    timestamped: list[tuple[datetime, Path]] = []
    for p in buf.iterdir():
        if p.suffix != ".json" or not p.is_file():
            continue
        timestamp = parse_capture_path_timestamp(p)
        if timestamp is not None and start <= timestamp < end:
            timestamped.append((timestamp, p))
    timestamped.sort(key=lambda item: item[0])
    return [path for _, path in timestamped]


def _load_captures(capture_files: list[Path]) -> list[tuple[Path, dict]]:
    """Parse every capture JSON once. Files that fail to read/parse are dropped.

    The window is small (≤30 files) so the entire parsed list stays cheap to
    pass around; the win is avoiding a second ``json.loads`` per file when
    ``_heuristic_entries`` runs after the LLM returns no usable output.
    """
    parsed: list[tuple[Path, dict]] = []
    for p in capture_files:
        # read_bytes() + json.loads handles BOM/encoding sniffing; read_text()
        # would raise UnicodeDecodeError (a ValueError, not OSError) on a
        # mis-encoded file and crash the aggregator instead of dropping it.
        try:
            data = json.loads(p.read_bytes())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("timeline: failed to load capture %s: %s", p.name, exc)
            continue
        if not isinstance(data, dict):
            logger.warning("timeline: capture %s is not a JSON object", p.name)
            continue
        # Sanitize once at the replay boundary so prompt rendering, heuristic
        # fallback, focus_excerpt, and per-app structured parsers all consume
        # the same placeholder-free projection.
        parsed.append((p, s1_parser.sanitize_capture(data, replace_ax_tree=True)))
    return parsed


def _format_events(
    parsed: list[tuple[Path, dict]],
    *,
    locus_enabled: bool = True,
    display_tz: tzinfo | None = None,
) -> tuple[str, list[str], AttentionLocus | None]:
    """Render captures for the timeline prompt.

    Returns ``(events_text, apps_used, block_locus)``. When ``locus_enabled``
    the per-capture ``| `` line is the resolved attention-locus content
    (``PRIMARY:`` / ``PERIPHERAL:``) — code-owned localization, chrome dropped
    for resolver-backed apps — and ``block_locus`` is the window's dominant
    locus (highest confidence, latest on ties) for persistence. When disabled
    it reproduces the pre-Step-1 feed (``FOCUSED PANE`` / raw visible_text) and
    ``block_locus`` is ``None``.

    Reads the structured S1 fields written by ``capture/s1_parser.py`` —
    ``focused_element``, ``visible_text``, ``url`` — and lays them out in
    the one-line-per-capture format matching Einsia's S1 prompt rendering.
    Pre-v2 captures without those fields fall back to a bounded
    ``ax_tree_to_markdown`` render so historical buffer contents still work.
    """
    lines: list[str] = []
    apps: set[str] = set()
    block_locus: AttentionLocus | None = None

    files = parsed[-_MAX_EVENTS_PER_WINDOW:]
    for i, (p, data) in enumerate(files, 1):
        # Direct unit callers can pass pre-parsed captures without going
        # through ``_load_captures``. Keep this idempotent boundary guard.
        data = s1_parser.sanitize_capture(data, replace_ax_tree=True)
        ts_raw = str(data.get("timestamp", p.stem))
        ts = _short_time(ts_raw, display_tz=display_tz)

        wm = data.get("window_meta") or {}
        app = str(wm.get("app_name") or "Unknown")
        title = str(wm.get("title") or "")
        bundle = str(wm.get("bundle_id") or "")
        if app:
            apps.add(app)

        trigger = data.get("trigger") or {}
        event_type = str(trigger.get("event_type") or "")

        parts = [f"{i}. [{ts}] {app}"]
        if title:
            parts.append(f"— {title}")
        if bundle:
            parts.append(f"({bundle})")

        url = data.get("url")
        if url:
            parts.append(f"(URL: {url})")

        fe = data.get("focused_element") or {}
        role = str(fe.get("role") or "")
        if role:
            role_desc = f"[{role}]"
            if fe.get("is_editable"):
                role_desc += " (editing)"
            fe_title = str(fe.get("title") or "")
            if fe_title:
                role_desc += f" title={fe_title[:80]}"
            value_length = int(fe.get("value_length") or 0)
            if value_length:
                role_desc += f" len={value_length}"
            value = str(fe.get("value") or "")
            if value:
                role_desc += f": {value}"
            parts.append(role_desc)

        if event_type:
            parts.append(f"<{event_type}>")

        # Attention anchor: for a pointer event the watcher hit-tests the AX
        # element directly under the cursor and ships it on trigger.details.
        # This is the "what did the user point at" signal — the strongest
        # focus cue in AX-opaque apps (terminals) where focused_element is
        # empty. Render the element so the normalizer can anchor the entry on
        # the clicked target instead of the window chrome.
        anchor = _click_anchor(trigger)
        if anchor:
            parts.append(anchor)

        lines.append(" ".join(parts))

        visible_text = data.get("visible_text")
        if visible_text is None:
            # Pre-v2 capture — fall back to rendering the raw AX tree.
            ax = data.get("ax_tree")
            visible_text = ax_tree_to_markdown(ax) if ax else ""
        visible_text = str(visible_text).strip()

        # If AX produced no text but OCR was submitted, use the OCR result.
        if not visible_text and data.get("ocr_submitted"):
            try:
                with fts_store.cursor() as conn:
                    ocr_text = fts_store.get_ocr_result_for_capture(conn, p.stem)
                    if ocr_text:
                        visible_text = s1_parser.sanitize_ocr_text(data, ocr_text).strip()
            except Exception:  # noqa: BLE001
                pass

        if visible_text:
            fe_value = str((data.get("focused_element") or {}).get("value") or "")
            if locus_enabled:
                # Code owns "where": resolve the attention locus and feed ITS
                # content (chrome dropped for resolver-backed apps) instead of
                # the raw dump. Track the window's dominant locus (highest
                # confidence, latest on ties) for persistence on the block.
                loc = resolve_locus(data, visible_text=visible_text)
                if block_locus is None or loc.confidence >= block_locus.confidence:
                    block_locus = loc
                primary = _slice_visible_text(
                    loc.content, fe_value, bundle, _PER_CAPTURE_TEXT_LIMIT
                )
                lines.append(f"| PRIMARY: {primary.replace(chr(10), ' ')}")
                if loc.peripheral:
                    per = _slice_visible_text(
                        loc.peripheral, "", bundle, _PER_CAPTURE_TEXT_LIMIT // 2
                    )
                    lines.append(f"| PERIPHERAL: {per.replace(chr(10), ' ')}")
            else:
                pane, focused = _focus_pane(visible_text)
                pane = _slice_visible_text(pane, fe_value, bundle, _PER_CAPTURE_TEXT_LIMIT)
                preview = pane.replace("\n", " ")
                label = "FOCUSED PANE: " if focused else ""
                lines.append(f"| {label}{preview}")

        lines.append("")
    return "\n".join(lines).strip(), sorted(apps), block_locus


def _short_time(ts: str, *, display_tz: tzinfo | None = None) -> str:
    """`2026-04-21T17:07:32+08:00` → `17:07:32`. Best-effort only."""
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is not None:
            dt = dt.astimezone(display_tz) if display_tz is not None else dt.astimezone()
        return dt.strftime("%H:%M:%S")
    except ValueError:
        return ts[:19]


def _format_window(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


_SKILL_CONFIDENCE_FLOOR = 0.65


def _validate_skill_hint(raw: object, *, skill_paths: set[str]) -> dict | None:
    """Coerce one skill_hints element into canonical form, or None to drop it."""
    if not isinstance(raw, dict):
        return None
    skill = str(raw.get("skill") or "").strip()
    if not skill or skill not in skill_paths:
        return None
    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        return None
    if confidence < _SKILL_CONFIDENCE_FLOOR:
        return None
    confidence = max(0.0, min(1.0, confidence))
    rationale = str(raw.get("rationale") or "").strip()
    if not rationale:
        return None
    return {"skill": skill, "confidence": confidence, "rationale": rationale}


def _echo_skill_hints(block: store.TimelineBlock, skill_hints: list[dict]) -> None:
    """Append a triggered-echo entry to each matched skill file."""
    for hint in skill_hints:
        skill_name = hint["skill"].removesuffix(".md")
        content = (
            f"Triggered with confidence {hint['confidence']:.2f}: {hint['rationale']}. "
            f"Context: {', '.join(block.apps_used)} at {block.start_time.strftime('%H:%M')}."
        )
        try:
            with fts_store.cursor() as conn:
                entries_mod.append_entry(
                    conn,
                    name=skill_name,
                    content=content,
                    tags=["triggered", "echo"],
                )
        except FileNotFoundError:
            logger.warning("timeline: skill file %s not found, skipping echo", hint["skill"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("timeline: failed to echo skill hint to %s: %s", hint["skill"], exc)


def produce_block_for_window(
    cfg: Config,
    *,
    start: datetime,
    end: datetime,
) -> store.TimelineBlock | None:
    """Build one block. Returns ``None`` if the window is empty or already done.

    Opens its own DB connections so it is safe to call from multiple threads
    in parallel. The has-window check and the final insert use separate
    short-lived connections; no connection is held open during the LLM call.
    """
    with fts_store.cursor() as conn:
        if store.has_window(conn, start, end):
            logger.debug(
                "timeline: window %s → %s already has a block", start.isoformat(), end.isoformat()
            )
            return None

    capture_files = captures_in_window(start, end)
    if not capture_files:
        logger.info(
            "timeline: window %s → %s has 0 captures, skipping",
            start.isoformat(),
            end.isoformat(),
        )
        return None

    # Parse capture JSON once; reused for prompt rendering AND the heuristic
    # fallback so an LLM miss doesn't trigger a second pass over the same files.
    parsed = _load_captures(capture_files)
    events_text, apps_used, block_locus = _format_events(
        parsed,
        locus_enabled=cfg.timeline.attention_locus_enabled,
        display_tz=start.tzinfo,
    )
    # Use len(parsed) — capture_count must match what the LLM actually sees
    # and what _heuristic_entries can group; len(capture_files) overcounts
    # whenever _load_captures drops a corrupt or non-dict file.
    capture_count = len(parsed)

    skill_rows: list = []
    if cfg.skill_check.enabled:
        with fts_store.cursor() as conn:
            skill_rows = [f for f in fts_store.list_files(conn) if f.path.startswith("skill-")]
    skill_paths = {r.path for r in skill_rows}
    if skill_rows:
        skill_index_section = "\n\n## Registered Skills\n\n" + "\n".join(
            f"- {r.path}: {r.description}" for r in skill_rows
        )
    else:
        skill_index_section = ""

    system_text = load_prompt("timeline_block.system.md")
    user_text = load_prompt("timeline_block.user.md").format(
        start_time=_format_window(start),
        end_time=_format_window(end),
        capture_count=capture_count,
        events_text=events_text,
        skill_index_section=skill_index_section,
    )

    entries: list[str] = []
    skill_hints: list[dict] = []
    action_trace: list[dict] = []
    try:
        resp = llm_mod.call_llm(
            cfg,
            "timeline",
            messages=[
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "text",
                            "text": system_text,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                },
                {"role": "user", "content": user_text},
            ],
            json_mode=True,
        )
        text = llm_mod.extract_text(resp).strip()
        data = json.loads(text) if text else {}
        if isinstance(data, dict):
            raw_entries = data.get("entries")
            if isinstance(raw_entries, list):
                entries = [str(e).strip() for e in raw_entries if str(e).strip()]
            raw_skills = data.get("skill_hints")
            if isinstance(raw_skills, list) and skill_paths:
                for raw in raw_skills:
                    validated_skill = _validate_skill_hint(raw, skill_paths=skill_paths)
                    if validated_skill is not None:
                        skill_hints.append(validated_skill)
                    else:
                        logger.debug("timeline: dropped malformed skill hint: %r", raw)
            raw_trace = data.get("action_trace")
            if isinstance(raw_trace, list):
                action_trace = [r for r in raw_trace if isinstance(r, dict)]
    except json.JSONDecodeError as exc:
        logger.warning("timeline: malformed JSON from LLM: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("timeline: LLM call failed: %s", exc)

    if not entries:
        entries = _heuristic_entries(parsed)

    focus_structured, parser_bundle, parser_outcome, _parser_miss_reason = (
        _focus_structured_with_outcome(parsed)
    )
    block = store.TimelineBlock(
        start_time=start,
        end_time=end,
        timezone=start.tzname() or "",
        entries=entries,
        apps_used=apps_used,
        capture_count=capture_count,
        skill_hints=skill_hints,
        action_trace=action_trace,
        focus_excerpt=_focus_excerpt(parsed),
        focus_structured=focus_structured,
        attention_surface=(block_locus.surface if block_locus else ""),
        attention_confidence=(block_locus.confidence if block_locus else 0.0),
        attention_rung=(block_locus.rung if block_locus else ""),
    )
    with fts_store.cursor() as conn:
        store.insert(conn, block)
        # Parser-hit telemetry (general observability): one tick per window that
        # had something parseable. Records hit/miss/fallback bucketed by bundle
        # so we can prove the per-app parsers are firing and catch semantic-class

        # production: a telemetry write failure is logged and swallowed.
        if parser_outcome is not None:
            try:
                parser_ticks.record_tick(
                    conn,
                    ts=start.isoformat(),
                    bundle_id=parser_bundle or "",
                    outcome=parser_outcome,
                )
            except Exception as exc:  # noqa: BLE001 - telemetry must not break ingestion
                logger.warning("timeline: parser tick record failed: %s", exc)
    logger.info(
        "timeline: stored block %s — %s → %s (%d entries, %d captures, %d skills, %d actions, apps=%s)",
        block.id,
        start.isoformat(),
        end.isoformat(),
        len(entries),
        capture_count,
        len(skill_hints),
        len(action_trace),
        ", ".join(apps_used),
    )
    if skill_hints:
        _echo_skill_hints(block, skill_hints)
    return block


def _heuristic_entries(parsed: list[tuple[Path, dict]]) -> list[str]:
    """Cheap fallback when the LLM returns no parseable entries."""
    groups: list[tuple[str, str, int]] = []
    for _p, data in parsed:
        wm = data.get("window_meta") or {}
        app = str(wm.get("app_name") or "Unknown")
        title = str(wm.get("title") or "")
        if groups and groups[-1][0] == app and groups[-1][1] == title:
            groups[-1] = (app, title, groups[-1][2] + 1)
        else:
            groups.append((app, title, 1))

    entries: list[str] = []
    for app, title, _count in groups:
        if title:
            entries.append(f"[{app}] worked in window '{title}', involving —")
        else:
            entries.append(f"[{app}] active, involving —")
    return entries
