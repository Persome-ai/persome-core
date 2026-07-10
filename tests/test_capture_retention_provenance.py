"""Extended screenshot retention for user-input-anchored capture receipts."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from persome.capture import scheduler as scheduler_mod
from persome.capture import screenshot_crypto

_HOUR = 3600
_DAY = 86400


def _capture_dict(
    *,
    ts: str,
    text: str = "hello",
    enc: bool = False,
    trigger_type: str = "manual",
) -> dict:
    shot: dict = {
        "image_base64": "Y2lwaGVydGV4dA==" if not enc else "PSOMEGCM1:Y2lwaGVydGV4dA==",
        "mime_type": "image/jpeg",
        "width": 100,
        "height": 50,
    }
    if enc:
        shot["screenshot_enc"] = True
    return {
        "timestamp": ts,
        "schema_version": 2,
        "trigger": {"event_type": trigger_type},
        "window_meta": {"app_name": "Cursor", "title": "main.py", "bundle_id": "test"},
        "focused_element": {"role": "AXTextArea", "value": text, "is_editable": True},
        "visible_text": text,
        "url": "",
        "screenshot": shot,
    }


def _write(out: dict) -> Path:
    return scheduler_mod._write_capture(out)


def _age_file(path: Path, *, seconds_old: float) -> None:
    timestamp = time.time() - seconds_old
    os.utime(path, (timestamp, timestamp))


def _has_screenshot(path: Path) -> bool:
    return "screenshot" in json.loads(path.read_text())


def test_extended_retention_off_strips_input_anchor(ac_root: Path) -> None:
    path = _write(_capture_dict(ts="2026-04-22T14:00:00+08:00", trigger_type="UserTextInput"))
    _age_file(path, seconds_old=30 * _HOUR)
    stats = scheduler_mod.cleanup_buffer(retention_hours=72, screenshot_retention_hours=24)
    assert stats["stripped"] == 1
    assert not _has_screenshot(path)


def test_input_anchor_kept_when_extended_retention_is_on(ac_root: Path) -> None:
    anchored = _write(_capture_dict(ts="2026-04-22T14:00:00+08:00", trigger_type="UserTextInput"))
    plain = _write(_capture_dict(ts="2026-04-22T15:00:00+08:00"))
    _age_file(anchored, seconds_old=30 * _HOUR)
    _age_file(plain, seconds_old=30 * _HOUR)
    stats = scheduler_mod.cleanup_buffer(
        retention_hours=72,
        screenshot_retention_hours=24,
        extended_retention_enabled=True,
        actionable_retention_days=7,
    )
    assert _has_screenshot(anchored)
    assert not _has_screenshot(plain)
    assert stats["stripped"] == 1


def test_input_anchor_strips_past_extended_cap(ac_root: Path) -> None:
    path = _write(_capture_dict(ts="2026-04-22T14:00:00+08:00", trigger_type="UserTextInput"))
    _age_file(path, seconds_old=8 * _DAY)
    stats = scheduler_mod.cleanup_buffer(
        retention_hours=30 * 24,
        screenshot_retention_hours=24,
        extended_retention_enabled=True,
        actionable_retention_days=7,
    )
    assert stats["stripped"] == 1
    assert not _has_screenshot(path)


def test_extended_retention_keeps_encrypted_bytes(ac_root: Path) -> None:
    path = _write(
        _capture_dict(
            ts="2026-04-22T14:00:00+08:00",
            enc=True,
            trigger_type="UserTextInput",
        )
    )
    before = json.loads(path.read_text())["screenshot"]
    _age_file(path, seconds_old=30 * _HOUR)
    scheduler_mod.cleanup_buffer(
        retention_hours=72,
        screenshot_retention_hours=24,
        extended_retention_enabled=True,
        actionable_retention_days=7,
    )
    after = json.loads(path.read_text())["screenshot"]
    assert after == before
    assert screenshot_crypto.is_encrypted(after["image_base64"])


def test_whole_file_delete_is_unchanged(ac_root: Path) -> None:
    path = _write(_capture_dict(ts="2026-04-22T14:00:00+08:00", trigger_type="UserTextInput"))
    _age_file(path, seconds_old=100 * _HOUR)
    stats = scheduler_mod.cleanup_buffer(
        retention_hours=72,
        screenshot_retention_hours=24,
        extended_retention_enabled=True,
        actionable_retention_days=7,
    )
    assert stats["deleted"] == 1
    assert not path.exists()
