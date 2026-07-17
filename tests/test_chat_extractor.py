"""Safe classifier drill-down over legacy capture-index rows."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from persome import paths
from persome.store import fts
from persome.writer.chat_extractor import extract_chat_messages
from persome.writer.tools import (
    CaptureEvidenceBounds,
    CommitState,
    dispatch,
    tool_drill_chat_captures,
)

_PHRASE = "Ask for follow-up changes"
_START = "2026-07-12T22:59:00+08:00"
_END = "2026-07-12T23:01:00+08:00"
_TZ = timezone(timedelta(hours=8))


def _legacy_placeholder_capture(*, ocr_submitted: bool = False) -> dict[str, Any]:
    return {
        "timestamp": "2026-07-12T23:00:00+08:00",
        "window_meta": {
            "app_name": "Chat",
            "title": "Conversation",
            "bundle_id": "com.example.chat",
        },
        "ocr_submitted": ocr_submitted,
        "focused_element": {
            "role": "AXTextArea",
            "value": _PHRASE,
            "is_editable": True,
        },
        "visible_text": "" if ocr_submitted else f"[TextArea] {_PHRASE}",
        "ax_tree": {
            "apps": [
                {
                    "name": "Chat",
                    "bundle_id": "com.example.chat",
                    "is_frontmost": True,
                    "focused_element": {
                        "role": "AXTextArea",
                        "value": _PHRASE,
                        "is_editable": True,
                    },
                    "windows": [
                        {
                            "title": "Conversation",
                            "elements": [
                                {"role": "AXStaticText", "value": "Existing conversation"},
                                {
                                    "role": "AXTextArea",
                                    "value": _PHRASE,
                                    "children": [
                                        {
                                            "role": "AXGroup",
                                            "domClassList": ["placeholder"],
                                            "children": [
                                                {"role": "AXStaticText", "value": _PHRASE}
                                            ],
                                        }
                                    ],
                                },
                            ],
                        }
                    ],
                }
            ]
        },
    }


def _seed_legacy_row(raw: dict[str, Any], *, indexed_text: str) -> None:
    capture_id = "2026-07-12T23-00-00p08-00"
    (paths.capture_buffer_dir() / f"{capture_id}.json").write_text(
        json.dumps(raw), encoding="utf-8"
    )
    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id=capture_id,
            timestamp=str(raw["timestamp"]),
            app_name="Chat",
            bundle_id="com.example.chat",
            window_title="Conversation",
            focused_role="AXTextArea",
            focused_value=_PHRASE,
            visible_text=indexed_text,
            url="",
        )


def test_legacy_placeholder_is_excluded_from_extractor_and_tool_drill(ac_root: Path) -> None:
    raw = _legacy_placeholder_capture()
    _seed_legacy_row(raw, indexed_text=f"[TextArea] {_PHRASE}")

    with fts.cursor() as conn:
        text, count, gaps = extract_chat_messages(conn, "Chat", _START, _END)
        drilled = tool_drill_chat_captures(conn, app_name="Chat", start_ts=_START, end_ts=_END)

    assert count == 1
    assert gaps == 0
    assert "Existing conversation" in text
    assert _PHRASE not in text
    assert drilled["ok"] is True
    assert drilled["snapshot_count"] == 1
    assert "Existing conversation" in drilled["content"]
    assert _PHRASE not in drilled["content"]


def test_tool_drill_sanitizes_placeholder_without_losing_db_only_ocr(ac_root: Path) -> None:
    raw = _legacy_placeholder_capture(ocr_submitted=True)
    _seed_legacy_row(raw, indexed_text=f"{_PHRASE}\nrecognized OCR body")

    with fts.cursor() as conn:
        drilled = tool_drill_chat_captures(conn, app_name="Chat", start_ts=_START, end_ts=_END)

    assert drilled["ok"] is True
    assert drilled["snapshot_count"] == 1
    assert "recognized OCR body" in drilled["content"]
    assert _PHRASE not in drilled["content"]


def test_filled_control_still_repairs_stale_placeholder_title_and_description(
    ac_root: Path,
) -> None:
    raw = _legacy_placeholder_capture()
    textarea = raw["ax_tree"]["apps"][0]["windows"][0]["elements"][1]
    textarea["value"] = "real draft"
    textarea["title"] = _PHRASE
    textarea["description"] = _PHRASE
    textarea["AXPlaceholderValue"] = _PHRASE
    focused = raw["ax_tree"]["apps"][0]["focused_element"]
    focused["value"] = "real draft"
    focused["title"] = _PHRASE
    focused["description"] = _PHRASE
    focused["AXPlaceholderValue"] = _PHRASE
    raw["focused_element"] = {
        "role": "AXTextArea",
        "title": _PHRASE,
        "value": "real draft",
        "is_editable": True,
    }
    raw["visible_text"] = f"[TextArea] {_PHRASE}: real draft"
    _seed_legacy_row(raw, indexed_text=raw["visible_text"])

    with fts.cursor() as conn:
        text, count, gaps = extract_chat_messages(conn, "Chat", _START, _END)

    assert count == 1
    assert gaps == 0
    assert "real draft" in text
    assert _PHRASE not in text


def test_missing_or_unproven_raw_capture_fails_open_to_indexed_text(ac_root: Path) -> None:
    raw = _legacy_placeholder_capture()
    raw["ax_tree"] = {"apps": []}
    raw["visible_text"] = "raw projection without AX proof"
    _seed_legacy_row(raw, indexed_text="DB text remains authoritative")

    with fts.cursor() as conn:
        text, count, _ = extract_chat_messages(conn, "Chat", _START, _END)

    assert count == 1
    assert "DB text remains authoritative" in text
    assert "raw projection without AX proof" not in text


def test_dispatch_intersects_classifier_drill_with_half_open_evidence_bounds(
    ac_root: Path,
) -> None:
    allowed_start = datetime(2026, 7, 12, 23, 0, tzinfo=_TZ)
    allowed_end = allowed_start + timedelta(minutes=1)
    with fts.cursor() as conn:
        for capture_id, moment, text in (
            ("before", allowed_start - timedelta(seconds=1), "BEFORE_BOUND_SECRET"),
            ("inside", allowed_start + timedelta(seconds=30), "INSIDE_BOUND_SAFE"),
            ("at-end", allowed_end, "AT_EXCLUSIVE_END_SECRET"),
        ):
            fts.insert_capture(
                conn,
                id=capture_id,
                timestamp=moment.isoformat(),
                app_name="Chat",
                bundle_id="com.example.chat",
                window_title="Conversation",
                focused_role="AXStaticText",
                focused_value=text,
                visible_text=text,
                url="",
            )
        result = dispatch(
            "drill_chat_captures",
            {
                "app_name": "Chat",
                "start_ts": (allowed_start - timedelta(minutes=1)).isoformat(),
                "end_ts": (allowed_end + timedelta(minutes=1)).isoformat(),
            },
            conn=conn,
            soft_limit_tokens=100,
            state=CommitState(),
            capture_evidence_bounds=CaptureEvidenceBounds(
                start=allowed_start,
                end=allowed_end,
            ),
        )
        rejected = dispatch(
            "drill_chat_captures",
            {
                "app_name": "Chat",
                "start_ts": (allowed_end + timedelta(minutes=1)).isoformat(),
                "end_ts": (allowed_end + timedelta(minutes=2)).isoformat(),
            },
            conn=conn,
            soft_limit_tokens=100,
            state=CommitState(),
            capture_evidence_bounds=CaptureEvidenceBounds(
                start=allowed_start,
                end=allowed_end,
            ),
        )

    assert result["ok"] is True
    assert result["bounds_clipped"] is True
    assert result["window_semantics"] == "[start, end)"
    assert result["start_ts"] == allowed_start.astimezone(UTC).isoformat()
    assert result["end_ts"] == allowed_end.astimezone(UTC).isoformat()
    assert "INSIDE_BOUND_SAFE" in result["content"]
    assert "BEFORE_BOUND_SECRET" not in result["content"]
    assert "AT_EXCLUSIVE_END_SECRET" not in result["content"]
    assert "outside the caller-owned evidence bounds" in rejected["error"]
