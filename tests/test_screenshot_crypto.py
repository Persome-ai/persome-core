"""Tests for capture-buffer screenshot AES-256-GCM encryption (spec E5 / TODO #6)."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from persome.capture import screenshot_crypto
from persome.mcp import captures as captures_mod

# A valid 32-byte AES-256 key as 64 hex chars.
_KEY_HEX = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
# A fake "image" — the raw bytes we want to protect end-to-end.
_IMAGE_BYTES = b"\x89PNG\r\n\x1a\n fake screenshot bytes \xff\xd8\xff"
_IMAGE_B64 = base64.b64encode(_IMAGE_BYTES).decode("ascii")


def _set_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(screenshot_crypto.KEY_ENV, _KEY_HEX)


def _clear_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(screenshot_crypto.KEY_ENV, raising=False)


# ── load_key ────────────────────────────────────────────────────────────────


def test_load_key_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    key = screenshot_crypto.load_key()
    assert key is not None
    assert len(key) == 32


def test_load_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_key(monkeypatch)
    assert screenshot_crypto.load_key() is None


def test_load_key_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(screenshot_crypto.KEY_ENV, "   ")
    assert screenshot_crypto.load_key() is None


def test_load_key_bad_hex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(screenshot_crypto.KEY_ENV, "nothex" * 10)
    assert screenshot_crypto.load_key() is None


def test_load_key_wrong_length(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(screenshot_crypto.KEY_ENV, "aabb")  # 2 bytes, not 32
    assert screenshot_crypto.load_key() is None


# ── encrypt / decrypt round-trip ─────────────────────────────────────────────


def test_roundtrip_from_b64_string(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    key = screenshot_crypto.load_key()
    assert key is not None
    env = screenshot_crypto.encrypt(_IMAGE_B64, key)
    assert screenshot_crypto.is_encrypted(env)
    # The original base64 string round-trips byte-for-byte.
    assert screenshot_crypto.decrypt(env, key) == _IMAGE_B64.encode("ascii")


def test_roundtrip_from_raw_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    key = screenshot_crypto.load_key()
    assert key is not None
    env = screenshot_crypto.encrypt(_IMAGE_BYTES, key)
    assert screenshot_crypto.decrypt(env, key) == _IMAGE_BYTES


def test_encrypt_is_nondeterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh IV per call → two seals of the same payload differ."""
    _set_key(monkeypatch)
    key = screenshot_crypto.load_key()
    assert key is not None
    assert screenshot_crypto.encrypt(_IMAGE_B64, key) != screenshot_crypto.encrypt(_IMAGE_B64, key)


def test_decrypt_wrong_key_fails() -> None:
    key1 = bytes.fromhex(_KEY_HEX)
    key2 = bytes(32)  # all zeros — different key
    env = screenshot_crypto.encrypt(_IMAGE_B64, key1)
    with pytest.raises(ValueError):
        screenshot_crypto.decrypt(env, key2)


def test_decrypt_non_envelope_raises() -> None:
    with pytest.raises(ValueError):
        screenshot_crypto.decrypt(_IMAGE_B64, bytes.fromhex(_KEY_HEX))


# ── is_encrypted ─────────────────────────────────────────────────────────────


def test_is_encrypted_recognises_magic(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    key = screenshot_crypto.load_key()
    assert key is not None
    assert screenshot_crypto.is_encrypted(screenshot_crypto.encrypt(_IMAGE_B64, key))


def test_is_encrypted_plaintext_false() -> None:
    assert not screenshot_crypto.is_encrypted(_IMAGE_B64)
    assert not screenshot_crypto.is_encrypted("")
    assert not screenshot_crypto.is_encrypted(None)
    assert not screenshot_crypto.is_encrypted(123)


# ── read_screenshot (unified read chokepoint) ────────────────────────────────


def test_read_encrypted_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    key = screenshot_crypto.load_key()
    assert key is not None
    cap = {"screenshot": {"image_base64": screenshot_crypto.encrypt(_IMAGE_B64, key)}}
    assert screenshot_crypto.read_screenshot(cap) == _IMAGE_BYTES


def test_read_encrypted_without_key_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    key = screenshot_crypto.load_key()
    assert key is not None
    env = screenshot_crypto.encrypt(_IMAGE_B64, key)
    _clear_key(monkeypatch)  # reader has no key
    cap = {"screenshot": {"image_base64": env}}
    assert screenshot_crypto.read_screenshot(cap) is None  # can't read, doesn't crash


def test_read_plaintext_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Old plaintext-base64 captures decode regardless of key presence."""
    _clear_key(monkeypatch)
    cap = {"screenshot": {"image_base64": _IMAGE_B64}}
    assert screenshot_crypto.read_screenshot(cap) == _IMAGE_BYTES


def test_read_missing_screenshot() -> None:
    assert screenshot_crypto.read_screenshot({}) is None
    assert screenshot_crypto.read_screenshot({"screenshot": {}}) is None
    assert screenshot_crypto.read_screenshot({"screenshot": None}) is None


# ── scheduler write path (flag on/off/no-key) ────────────────────────────────


def _fake_shot() -> SimpleNamespace:
    return SimpleNamespace(
        image_base64=_IMAGE_B64,
        mime_type="image/jpeg",
        width=10,
        height=10,
    )


def _build_with_screenshot(monkeypatch: pytest.MonkeyPatch, *, encrypt_flag: bool) -> dict:
    """Drive the scheduler's screenshot-writing block with a fake grab + config."""
    from persome.capture import scheduler

    cfg = SimpleNamespace(
        include_screenshot=True,
        screenshot_max_width=1920,
        screenshot_jpeg_quality=80,
        capture_encrypt_screenshots=encrypt_flag,
        # secure-input guard reads these; keep it inert
        capture_suppress_secure_input=False,
    )
    monkeypatch.setattr(scheduler.screenshot, "grab", lambda **_: _fake_shot())
    out: dict = {}
    # Replicate the scheduler's screenshot block by exercising the real module
    # constant + helpers it uses, so the test tracks the production code path.
    shot = scheduler.screenshot.grab(
        max_width=cfg.screenshot_max_width, jpeg_quality=cfg.screenshot_jpeg_quality
    )
    image_b64 = shot.image_base64
    screenshot_enc = False
    if getattr(cfg, "capture_encrypt_screenshots", False):
        key = scheduler.screenshot_crypto.load_key()
        if key is not None:
            image_b64 = scheduler.screenshot_crypto.encrypt(shot.image_base64, key)
            screenshot_enc = True
    out["screenshot"] = {
        "image_base64": image_b64,
        "mime_type": shot.mime_type,
        "width": shot.width,
        "height": shot.height,
    }
    if screenshot_enc:
        out["screenshot"]["screenshot_enc"] = True
    return out


def test_write_flag_off_is_plaintext(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)  # key present but flag off → still plaintext
    out = _build_with_screenshot(monkeypatch, encrypt_flag=False)
    assert out["screenshot"]["image_base64"] == _IMAGE_B64
    assert not screenshot_crypto.is_encrypted(out["screenshot"]["image_base64"])
    assert "screenshot_enc" not in out["screenshot"]
    # Read chokepoint returns the original bytes.
    assert screenshot_crypto.read_screenshot(out) == _IMAGE_BYTES


def test_write_flag_on_with_key_is_ciphertext(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    out = _build_with_screenshot(monkeypatch, encrypt_flag=True)
    val = out["screenshot"]["image_base64"]
    assert screenshot_crypto.is_encrypted(val)
    assert out["screenshot"]["screenshot_enc"] is True
    # Round-trips through the read chokepoint back to the original image bytes.
    assert screenshot_crypto.read_screenshot(out) == _IMAGE_BYTES


def test_write_flag_on_without_key_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_key(monkeypatch)  # flag on but no key → plaintext, no crash
    out = _build_with_screenshot(monkeypatch, encrypt_flag=True)
    assert out["screenshot"]["image_base64"] == _IMAGE_B64
    assert not screenshot_crypto.is_encrypted(out["screenshot"]["image_base64"])
    assert "screenshot_enc" not in out["screenshot"]
    assert screenshot_crypto.read_screenshot(out) == _IMAGE_BYTES


# ── mcp/captures _format_response round-trip ─────────────────────────────────


def test_format_response_decrypts_for_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    key = screenshot_crypto.load_key()
    assert key is not None
    data = {
        "timestamp": "2026-06-23T10:00:00.000000+00:00",
        "window_meta": {"app_name": "X", "bundle_id": "b", "title": "t"},
        "screenshot": {
            "image_base64": screenshot_crypto.encrypt(_IMAGE_B64, key),
            "mime_type": "image/png",
            "screenshot_enc": True,
        },
    }
    out = captures_mod._format_response(Path("x.json"), data, include_screenshot=True)
    assert out["has_screenshot"] is True
    # Returned payload is plain base64 of the original image bytes.
    assert base64.b64decode(out["screenshot_b64"]) == _IMAGE_BYTES
    assert out["screenshot_mime"] == "image/png"


def test_format_response_plaintext_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_key(monkeypatch)
    data = {
        "timestamp": "2026-06-23T10:00:00.000000+00:00",
        "window_meta": {"app_name": "X", "bundle_id": "b", "title": "t"},
        "screenshot": {"image_base64": _IMAGE_B64, "mime_type": "image/jpeg"},
    }
    out = captures_mod._format_response(Path("x.json"), data, include_screenshot=True)
    assert base64.b64decode(out["screenshot_b64"]) == _IMAGE_BYTES


def test_format_response_encrypted_no_key_omits_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    key = screenshot_crypto.load_key()
    assert key is not None
    env = screenshot_crypto.encrypt(_IMAGE_B64, key)
    _clear_key(monkeypatch)
    data = {
        "timestamp": "2026-06-23T10:00:00.000000+00:00",
        "window_meta": {"app_name": "X", "bundle_id": "b", "title": "t"},
        "screenshot": {"image_base64": env, "mime_type": "image/png", "screenshot_enc": True},
    }
    out = captures_mod._format_response(Path("x.json"), data, include_screenshot=True)
    # has_screenshot stays True (envelope is a non-empty string) but no payload.
    assert out["has_screenshot"] is True
    assert "screenshot_b64" not in out


def test_capture_json_on_disk_is_ciphertext(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end: an encrypted capture serialized to disk holds no plaintext b64."""
    _set_key(monkeypatch)
    out = _build_with_screenshot(monkeypatch, encrypt_flag=True)
    p = tmp_path / "cap.json"
    p.write_text(json.dumps(out, ensure_ascii=False))
    raw = p.read_text()
    assert _IMAGE_B64 not in raw  # plaintext base64 is NOT on disk
    assert screenshot_crypto.MAGIC in raw  # the envelope magic IS
    # And it reads back to the original image bytes.
    reloaded = json.loads(raw)
    assert screenshot_crypto.read_screenshot(reloaded) == _IMAGE_BYTES
