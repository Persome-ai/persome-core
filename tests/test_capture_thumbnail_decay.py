"""Pixel-axis graded forgetting — the §2.1 thumbnail tier in cleanup_buffer.

全分辨率 → 缩略 → 仅存文本化（strip）→ 删除. Deterministic; uses real PIL
images so the downscale path is exercised for real. Covers: the tier fires
only in its age window, off-by-default byte-equivalence, idempotence (marked
once, never re-read), encrypted round-trip (decrypt → downscale → re-encrypt)
and key-unavailable fail-open, small-image mark-only, corrupt-image fail-open,
actionable extended retention keeping FULL resolution, and strip winning over
thumbnail past the strip cutoff.
"""

from __future__ import annotations

import base64
import io
import json
import os
import time
from pathlib import Path

from PIL import Image

from persome.capture import scheduler as scheduler_mod
from persome.capture import screenshot_crypto

HOUR = 3600


def _jpeg_b64(width: int = 1600, height: int = 900) -> str:
    img = Image.new("RGB", (width, height), (200, 30, 30))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode()


def _capture(*, ts: str, b64: str | None = None, enc_key: bytes | None = None) -> dict:
    payload = b64 if b64 is not None else _jpeg_b64()
    shot: dict = {"mime_type": "image/jpeg", "width": 1600, "height": 900}
    if enc_key is not None:
        shot["image_base64"] = screenshot_crypto.encrypt(payload, enc_key)
        shot["screenshot_enc"] = True
    else:
        shot["image_base64"] = payload
    return {
        "timestamp": ts,
        "schema_version": 2,
        "trigger": {"event_type": "manual"},
        "window_meta": {"app_name": "Cursor", "title": "t", "bundle_id": "com.test"},
        "focused_element": {"role": "AXTextArea", "value": "x", "is_editable": True},
        "visible_text": "x",
        "url": "",
        "screenshot": shot,
    }


def _write_aged(out: dict, *, hours_old: float) -> Path:
    p = scheduler_mod._write_capture(out)
    t = time.time() - hours_old * HOUR
    os.utime(p, (t, t))
    return p


def _cleanup(**kw) -> dict:
    defaults = dict(
        retention_hours=168,
        processed_before_ts="9999-12-31T00:00:00",  # everything absorbed
        screenshot_retention_hours=24,
        screenshot_thumbnail_hours=6,
    )
    defaults.update(kw)
    return scheduler_mod.cleanup_buffer(
        defaults.pop("retention_hours"), defaults.pop("processed_before_ts"), **defaults
    )


def _shot(p: Path) -> dict:
    return json.loads(p.read_text()).get("screenshot") or {}


class TestThumbnailTier:
    def test_aged_capture_is_downscaled_in_place(self, ac_root):
        p = _write_aged(_capture(ts="2026-07-01T10:00:00"), hours_old=8)
        before = len(_shot(p)["image_base64"])
        stats = _cleanup()
        assert stats["thumbnailed"] == 1 and stats["stripped"] == 0
        shot = _shot(p)
        assert shot["thumbnail"] is True
        assert shot["width"] == scheduler_mod._THUMBNAIL_MAX_WIDTH
        assert len(shot["image_base64"]) < before  # bytes actually shed
        # the downscaled payload is a decodable JPEG of the recorded size
        img = Image.open(io.BytesIO(base64.b64decode(shot["image_base64"])))
        assert img.size == (shot["width"], shot["height"])

    def test_young_capture_untouched(self, ac_root):
        p = _write_aged(_capture(ts="2026-07-01T10:00:00"), hours_old=2)
        stats = _cleanup()
        assert stats["thumbnailed"] == 0
        assert "thumbnail" not in _shot(p)

    def test_disabled_by_default_is_byte_identical(self, ac_root):
        p = _write_aged(_capture(ts="2026-07-01T10:00:00"), hours_old=8)
        raw_before = p.read_text()
        stats = _cleanup(screenshot_thumbnail_hours=0)
        assert stats["thumbnailed"] == 0
        assert p.read_text() == raw_before

    def test_idempotent_second_pass_is_noop(self, ac_root):
        p = _write_aged(_capture(ts="2026-07-01T10:00:00"), hours_old=8)
        assert _cleanup()["thumbnailed"] == 1
        raw_after_first = p.read_text()
        t = time.time() - 8 * HOUR
        os.utime(p, (t, t))
        assert _cleanup()["thumbnailed"] == 0  # marked — never re-read
        assert p.read_text() == raw_after_first

    def test_past_strip_cutoff_strip_wins(self, ac_root):
        p = _write_aged(_capture(ts="2026-07-01T10:00:00"), hours_old=30)
        stats = _cleanup()
        assert stats["stripped"] == 1 and stats["thumbnailed"] == 0
        assert "screenshot" not in json.loads(p.read_text())

    def test_small_image_marked_without_reencode(self, ac_root):
        small = _jpeg_b64(width=320, height=200)
        p = _write_aged(_capture(ts="2026-07-01T10:00:00", b64=small), hours_old=8)
        assert _cleanup()["thumbnailed"] == 1
        shot = _shot(p)
        assert shot["thumbnail"] is True
        assert shot["image_base64"] == small  # no re-encode of an already-small image

    def test_corrupt_image_fail_open(self, ac_root):
        bad = base64.b64encode(b"not an image").decode()
        p = _write_aged(_capture(ts="2026-07-01T10:00:00", b64=bad), hours_old=8)
        stats = _cleanup()
        assert stats["thumbnailed"] == 0
        assert _shot(p)["image_base64"] == bad  # untouched — strip reaps later


class TestEncryptedRoundTrip:
    KEY = bytes(range(32))

    def test_encrypted_screenshot_downscaled_and_reencrypted(self, ac_root, monkeypatch):
        monkeypatch.setattr(screenshot_crypto, "load_key", lambda: self.KEY)
        p = _write_aged(_capture(ts="2026-07-01T10:00:00", enc_key=self.KEY), hours_old=8)
        assert _cleanup()["thumbnailed"] == 1
        shot = _shot(p)
        assert shot["thumbnail"] is True
        assert shot["screenshot_enc"] is True
        assert shot["image_base64"].startswith(screenshot_crypto.MAGIC)  # STAYS ciphertext
        # and the ciphertext opens to a real thumbnail
        inner = screenshot_crypto.decrypt(shot["image_base64"], self.KEY)
        img = Image.open(io.BytesIO(base64.b64decode(inner)))
        assert img.width == scheduler_mod._THUMBNAIL_MAX_WIDTH

    def test_key_unavailable_leaves_ciphertext_intact(self, ac_root, monkeypatch):
        p = _write_aged(_capture(ts="2026-07-01T10:00:00", enc_key=self.KEY), hours_old=8)
        monkeypatch.setattr(screenshot_crypto, "load_key", lambda: None)
        before = _shot(p)["image_base64"]
        assert _cleanup()["thumbnailed"] == 0
        assert _shot(p)["image_base64"] == before


class TestExtendedRetentionInteraction:
    def test_enter_anchored_capture_keeps_full_resolution(self, ac_root):
        out = _capture(ts="2026-07-01T10:00:00")
        out["trigger"] = {"event_type": "UserTextInput"}  # Enter-anchored (#7)
        p = _write_aged(out, hours_old=8)
        stats = _cleanup(extended_retention_enabled=True, actionable_retention_days=7)
        assert stats["thumbnailed"] == 0  # grounding frame stays full-res
        assert "thumbnail" not in _shot(p)


class TestTickWiring:
    def test_cleanup_once_forwards_thumbnail_hours(self, ac_root, monkeypatch):
        from persome import config as config_mod
        from persome.timeline import tick as tick_mod

        seen: dict = {}

        def fake_cleanup(retention_hours, processed=None, **kw):
            seen.update(kw, retention_hours=retention_hours)
            return {"deleted": 0, "stripped": 0, "thumbnailed": 0, "evicted": 0}

        monkeypatch.setattr(tick_mod.capture_scheduler, "cleanup_buffer", fake_cleanup)
        cfg = config_mod.Config()
        cfg.capture.screenshot_thumbnail_hours = 6
        tick_mod._cleanup_buffer_once(cfg)
        assert seen["screenshot_thumbnail_hours"] == 6
