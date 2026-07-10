"""Read-side helper for ``read_recent_capture``.

Reads JSON files straight out of ``~/.persome/capture-buffer/`` and
returns the closest match to an optional timestamp with optional app / title
filters. Filenames are ISO timestamps (``:`` → ``-``, ``+`` → ``p``,
``-`` → ``m`` in the offset), which is enough to pre-filter by name before
opening the JSON — critical because each JSON is ~160 KB.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import paths
from ..capture import screenshot_crypto
from ..store import fts as fts_store

# Agent-Native firewall: captures are THIRD-PARTY screen content, so every capture/context tool
# return is tagged with this provenance — DATA the agent reads, never instructions
# (spec 2026-06-25-agent-native-persome-design §7). One definition for all return sites.
CAPTURE_PROVENANCE = "observed"


def _parse_stem(stem: str) -> datetime | None:
    """Invert ``scheduler._safe_filename``. Returns None on malformed input."""
    try:
        date_part, _, rest = stem.partition("T")
        if not rest:
            return None
        for sign, marker in (("+", "p"), ("-", "m")):
            if marker in rest:
                time_part, _, offset = rest.partition(marker)
                h, m, s = time_part.split("-")
                oh, om = offset.split("-")
                return datetime.fromisoformat(f"{date_part}T{h}:{m}:{s}{sign}{oh}:{om}")
        return None
    except (ValueError, IndexError):
        return None


def _parse_at(text: str) -> datetime:
    """Accept ISO timestamps or bare ``HH:MM[:SS]``. Bare times use today (local)."""
    s = text.strip()
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        now = datetime.now().astimezone()
        today = now.date()
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                t = datetime.strptime(s, fmt).time()
                return datetime.combine(today, t, tzinfo=now.tzinfo)
            except ValueError:
                continue
        raise ValueError(f"cannot parse time: {text!r}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt


def _matches(
    data: dict[str, Any],
    app_name: str | None,
    window_title_substring: str | None,
) -> bool:
    if not app_name and not window_title_substring:
        return True
    meta = data.get("window_meta") or {}
    name = (meta.get("app_name") or "").lower()
    title = (meta.get("title") or "").lower()
    if app_name and app_name.lower() not in name:
        return False
    return not (window_title_substring and window_title_substring.lower() not in title)


def _load_capture(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())  # type: ignore[no-any-return]
    except (OSError, json.JSONDecodeError):
        return None


def _count_ax_nodes(ax_tree: Any) -> int:
    """Bounded count of AX-element-like dicts in the raw ax_tree.

    A rough "how much did AX see" metric for capture-context consumers. Counts dicts
    that look like an AX element (carry a role / children key). Iterative +
    capped so a pathological tree can't blow up a context read.
    """
    if not isinstance(ax_tree, (dict, list)):
        return 0
    count = 0
    stack: list[Any] = [ax_tree]
    while stack and count < 100_000:
        cur = stack.pop()
        if isinstance(cur, dict):
            if any(k in cur for k in ("role", "AXRole", "children", "AXChildren")):
                count += 1
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return count


def _ax_has_content(visible_text: str) -> bool:
    """Mirror scheduler's check: indented bullet lines mean AX produced real
    content; a header-only frame (``## App [active]`` …) has none."""
    return any(line.startswith("  ") for line in (visible_text or "").split("\n"))


def _resolve_text(result: dict[str, Any], data: dict[str, Any], capture_id: str) -> dict[str, Any]:
    """Split the one ``visible_text`` field into its real provenance.

    The capture JSON stores the AX-derived text; on-device OCR backfills
    ``captures.visible_text`` (DB) only when the AX text was empty (see
    ``scheduler._submit_ocr_async`` + ``fts.backfill_capture_ocr_text``). So:

      * AX text present in the JSON  → ``text_source="ax"`` (may be header-only).
      * AX text empty + DB has text  → ``text_source="ocr"`` (WeChat & co.).
      * both empty                   → ``text_source="none"``.

    ``visible_text`` stays the resolved text (AX else OCR) for back-compat, and
    we surface an ``ocr`` status block so clients can explain WHY a window
    is blank (OCR not run / submitted-but-empty / recognized).
    """
    ax_text = data.get("visible_text") or ""
    ocr_text = ""
    if not ax_text:
        try:
            with fts_store.cursor() as conn:
                ocr_text = fts_store.get_ocr_result_for_capture(conn, capture_id) or ""
        except Exception:  # noqa: BLE001
            ocr_text = ""

    if ax_text:
        source = "ax"
    elif ocr_text:
        source = "ocr"
    else:
        source = "none"

    submitted = bool(data.get("ocr_submitted"))
    if ocr_text:
        ocr_state = "recognized"
    elif submitted:
        ocr_state = "submitted_empty"  # OCR ran on the screenshot but read nothing
    else:
        ocr_state = "not_run"  # AX had content, OCR throttled, or not an OCR app

    result["ax_text"] = ax_text
    result["ocr_text"] = ocr_text
    result["visible_text"] = ax_text or ocr_text
    result["text_source"] = source
    result["text_chars"] = len(result["visible_text"])
    result["ocr"] = {"submitted": submitted, "status": ocr_state, "chars": len(ocr_text)}
    return result


def _format_response(
    path: Path,
    data: dict[str, Any],
    include_screenshot: bool,
    include_ax_tree: bool = False,
) -> dict[str, Any]:
    meta = data.get("window_meta") or {}
    focused = data.get("focused_element") or {}
    shot = data.get("screenshot") or {}
    trigger = data.get("trigger") or {}
    ax_meta = data.get("ax_metadata") or {}
    ax_tree = data.get("ax_tree")
    ax_text = data.get("visible_text") or ""
    out: dict[str, Any] = {
        "provenance": CAPTURE_PROVENANCE,
        "timestamp": data.get("timestamp"),
        "file": path.name,
        "file_stem": path.stem,
        "app_name": meta.get("app_name"),
        "bundle_id": meta.get("bundle_id"),
        "window_title": meta.get("title"),
        "url": data.get("url"),
        "focused_element": {
            "role": focused.get("role") or "",
            "title": focused.get("title") or "",
            "value": focused.get("value") or "",
            "is_editable": bool(focused.get("is_editable")),
            "value_length": int(focused.get("value_length") or 0),
        },
        "visible_text": ax_text,
        "screenshot_stripped": bool(data.get("screenshot_stripped")),
        # ── capture status / provenance ──────────────────────────────────
        "trigger": trigger.get("event_type") if isinstance(trigger, dict) else None,
        "schema_version": data.get("schema_version"),
        "ax": {
            "present": bool(ax_tree),
            "has_content": _ax_has_content(ax_text),
            "node_count": _count_ax_nodes(ax_tree),
            "mode": ax_meta.get("mode"),
            "depth": ax_meta.get("depth"),
        },
        "has_screenshot": bool(shot.get("image_base64")),
        "cmux_text_injected": bool(data.get("cmux_text_injected")),
    }
    if include_screenshot and shot.get("image_base64"):
        # Route through the crypto chokepoint: decrypts a sealed envelope when a
        # key is present, decodes plaintext base64 otherwise. Re-encode to the
        # plain base64 string this response contract expects. A key-less read of
        # an encrypted shot yields None → omit the field (caller sees no payload).
        img_bytes = screenshot_crypto.read_screenshot(data)
        if img_bytes is not None:
            out["screenshot_b64"] = base64.b64encode(img_bytes).decode("ascii")
            out["screenshot_mime"] = shot.get("mime_type") or "image/jpeg"
    if include_ax_tree:
        # Progressive disclosure: the full structured tree, incl. the browser
        # chrome (bookmarks / tabs / extensions) that visible_text folds into a
        # one-line digest. Large + opt-in — for an agent that needs to "expand".
        out["ax_tree"] = ax_tree
    return out


def read_recent_capture(
    *,
    at: str | None = None,
    app_name: str | None = None,
    window_title_substring: str | None = None,
    include_screenshot: bool = False,
    include_ax_tree: bool = False,
    max_age_minutes: int = 15,
) -> dict[str, Any] | None:
    """Return the capture that best matches the given time + filters.

    ``at`` None → newest matching capture overall.
    ``at`` set → nearest-in-time match, bounded by ``max_age_minutes`` on either side.
    """
    buf = paths.capture_buffer_dir()
    if not buf.exists():
        return None

    target: datetime | None = _parse_at(at) if at else None

    # Filenames sort lexicographically by wall-clock time; pre-filter by name
    # range so we don't open hundreds of JSONs we don't need.
    stems = sorted(
        (p for p in buf.iterdir() if p.is_file() and p.suffix == ".json"),
        reverse=target is None,  # newest-first when no anchor time
    )

    best: tuple[float, Path, dict[str, Any]] | None = None

    for path in stems:
        ts = _parse_stem(path.stem)
        if ts is None:
            continue
        if target is not None:
            delta = abs((ts - target).total_seconds())
            if delta > max_age_minutes * 60:
                # With no ordering guarantee across timezones we can't short-
                # circuit, but the buffer is small enough post-cleanup that
                # a full pass is cheap.
                continue
        data = _load_capture(path)
        if data is None:
            continue
        if not _matches(data, app_name, window_title_substring):
            continue
        if target is None:
            return _resolve_text(
                _format_response(path, data, include_screenshot, include_ax_tree), data, path.stem
            )
        delta = abs((ts - target).total_seconds())
        if best is None or delta < best[0]:
            best = (delta, path, data)

    if best is None:
        return None
    _, path, data = best
    return _resolve_text(
        _format_response(path, data, include_screenshot, include_ax_tree), data, path.stem
    )


def read_capture_by_stem(
    stem: str,
    *,
    include_screenshot: bool = False,
    include_ax_tree: bool = False,
) -> dict[str, Any] | None:
    """Exact capture lookup by file stem (the ``file_stem`` headlines carry).

    The HH:MM ``at`` path only resolves to the *nearest* capture for an app
    within a tolerance window, so two captures in the same minute (or a shared
    cwd) can mis-resolve. The caller already knows the exact stem, so this
    avoids that whole class of cross-attribution.
    """
    if not stem or "/" in stem or "\\" in stem or ".." in stem:
        return None
    path = paths.capture_buffer_dir() / f"{stem}.json"
    if not path.is_file():
        return None
    data = _load_capture(path)
    if data is None:
        return None
    return _resolve_text(
        _format_response(path, data, include_screenshot, include_ax_tree), data, path.stem
    )


# ─── search_captures + current_context (FTS-backed) ───────────────────────


def search_captures(
    *,
    query: str,
    since: str | None = None,
    until: str | None = None,
    app_name: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """BM25 + snippet search over the S1 FTS index.

    Returns a list of light-weight hits — `file_stem` is the handle to follow
    up with `read_recent_capture(at=<timestamp>, app_name=<app>)` for the
    full visible_text + screenshot.
    """
    with fts_store.cursor() as conn:
        hits = fts_store.search_captures(
            conn,
            query=query,
            since=since,
            until=until,
            app_name=app_name,
            limit=limit,
        )
    return [
        {
            "provenance": CAPTURE_PROVENANCE,
            "timestamp": h.timestamp,
            "app_name": h.app_name,
            "bundle_id": h.bundle_id,
            "window_title": h.window_title,
            "url": h.url,
            "snippet": h.snippet,
            "rank": h.rank,
            "file_stem": h.id,
            "focused_role": h.focused_role,
            "focused_value_preview": (h.focused_value or "")[:200],
        }
        for h in hits
    ]


def _dedupe_recent_captures(
    rows: list[fts_store.CaptureHit],
    *,
    limit: int,
) -> list[fts_store.CaptureHit]:
    """Pick up to ``limit`` rows distinct by (app_name, window_title)."""
    seen: set[tuple[str, str]] = set()
    out: list[fts_store.CaptureHit] = []
    for r in rows:
        key = (r.app_name or "", r.window_title or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
        if len(out) >= limit:
            break
    return out


def _recent_timeline_blocks(
    conn: sqlite3.Connection,
    limit: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT start_time, end_time, entries, apps_used, capture_count
          FROM timeline_blocks
         ORDER BY end_time DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            entries = json.loads(r["entries"] or "[]")
        except json.JSONDecodeError:
            entries = []
        try:
            apps = json.loads(r["apps_used"] or "[]")
        except json.JSONDecodeError:
            apps = []
        out.append(
            {
                "start_time": r["start_time"],
                "end_time": r["end_time"],
                "entries": entries,
                "apps_used": apps,
                "capture_count": r["capture_count"] or 0,
            }
        )
    # Newest first looks weird in a context block; reverse to time-ordered.
    return list(reversed(out))


def current_context(
    *,
    app_filter: str | None = None,
    headline_limit: int = 5,
    fulltext_limit: int = 3,
    timeline_limit: int = 8,
) -> dict[str, Any]:
    """One-shot snapshot of "what's happening on screen right now".

    Mirrors the payload Einsia-Partner auto-injects every chat turn:

      * ``recent_captures_headline`` — last N captures as ``[HH:MM] App — Title [Role]``
      * ``recent_captures_fulltext`` — top M captures deduped by (app, window),
        carrying the FULL visible_text + focused_element.value so the model can
        actually read what's on screen
      * ``recent_timeline_blocks`` — the last K LLM-summarized 1-min blocks
    """
    with fts_store.cursor() as conn:
        rows = fts_store.recent_captures(
            conn,
            app_name=app_filter,
            limit=max(headline_limit, 30),
        )
        full_rows = _dedupe_recent_captures(rows, limit=fulltext_limit)
        full: list[dict[str, Any]] = []
        for r in full_rows:
            visible = fts_store.get_capture_visible_text(conn, r.id)
            full.append(
                {
                    "timestamp": r.timestamp,
                    "app_name": r.app_name,
                    "window_title": r.window_title,
                    "url": r.url,
                    "focused_role": r.focused_role,
                    "focused_value": r.focused_value,
                    "visible_text": visible,
                    "file_stem": r.id,
                }
            )
        timeline = _recent_timeline_blocks(conn, timeline_limit)
        # Lightweight per-headline preview so a context list shows content
        # (incl. OCR-backfilled WeChat text) at a glance. One indexed SELECT per
        # headline (≤ headline_limit) — cheap; no JSON reads on this hot path.
        head_text: dict[str, str] = {
            r.id: (fts_store.get_capture_visible_text(conn, r.id) or "")
            for r in rows[:headline_limit]
        }

    headlines: list[dict[str, Any]] = []
    for r in rows[:headline_limit]:
        ts_short = (r.timestamp or "")[11:16]  # HH:MM from ISO
        vt = head_text.get(r.id, "")
        preview = " ".join(vt.split())[:120]
        headlines.append(
            {
                "time": ts_short,
                "app_name": r.app_name,
                "window_title": r.window_title,
                "focused_role": r.focused_role,
                "file_stem": r.id,
                "preview": preview,
                "text_chars": len(vt),
            }
        )

    return {
        "provenance": CAPTURE_PROVENANCE,
        "recent_captures_headline": headlines,
        "recent_captures_fulltext": full,
        "recent_timeline_blocks": timeline,
    }
