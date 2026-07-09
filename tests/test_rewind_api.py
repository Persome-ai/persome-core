"""Tests for the Rewind (截图回放) read-only REST endpoints (spec E6/#9).

`GET /rewind/day?date=` returns the day's timeline blocks each tagged with its
window's capture stems; `GET /rewind/screenshot?stem=` returns the decrypted
screenshot bytes (or 404). Both are gated behind `rewind_enabled` (default off)
and live behind the #5 Origin/Host guard (exercised in test_api_origin_guard).
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from persome.api import build_api_app
from persome.capture import screenshot_crypto
from persome.config import Config
from persome.store import fts
from persome.timeline import store as timeline_store
from persome.timeline.store import TimelineBlock

# A tiny but valid JPEG-ish payload — content is opaque to the endpoint, which only
# relays the bytes read_screenshot returns. The day view detects has_screenshot from the
# `"image_base64"` marker in the raw capture, so size is irrelevant here.
_IMG_BYTES = b"\xff\xd8\xff\xe0fake-jpeg-bytes\xff\xd9"
_IMG_B64 = base64.b64encode(_IMG_BYTES).decode("ascii")
# A valid 32-byte (64 hex) AES-256 key for the encrypted-capture case.
_HEX_KEY = "11" * 32


def _rewind_cfg(enabled: bool = True) -> Config:
    cfg = Config()
    cfg.rewind_enabled = enabled  # type: ignore[attr-defined]
    return cfg


def _local_client(cfg: Config) -> TestClient:
    """Client with a local Host header so the #5 origin guard passes.

    Wires the route module's config (the same seam ``register_routes`` uses in
    the daemon) so the endpoints resolve ``rewind_enabled`` off the passed cfg.
    """
    from persome.api import routes as routes_mod

    routes_mod.set_config(cfg)
    return TestClient(build_api_app(cfg), headers={"host": "127.0.0.1:8773"})


def _stem_for(dt: datetime) -> str:
    """Build a capture stem the aggregator's _stem_to_dt round-trips.

    Mirrors scheduler's sanitisation (``:`` → ``-``, ``+`` → ``p``) plus the
    ``-`` → ``m`` swap the aggregator expects for negative offsets.
    """
    iso = dt.replace(microsecond=0).isoformat()
    date_part, rest = iso[:10], iso[11:]
    # rest = "HH:MM:SS+HH:MM" (or "-HH:MM"); split the offset off the time
    if "+" in rest:
        time_part, off = rest.split("+", 1)
        off_tag = "p" + off.replace(":", "-")
    elif "-" in rest:
        time_part, off = rest.split("-", 1)
        off_tag = "m" + off.replace(":", "-")
    else:
        time_part, off_tag = rest, ""
    return f"{date_part}T{time_part.replace(':', '-')}{off_tag}"


def _write_capture(stem: str, *, screenshot: str | None) -> None:
    from persome import paths

    data: dict = {"timestamp": stem, "window_meta": {"app_name": "TestApp"}}
    if screenshot is not None:
        data["screenshot"] = {"image_base64": screenshot}
    (paths.capture_buffer_dir() / f"{stem}.json").write_text(
        json.dumps(data), encoding="utf-8"
    )


def _block(start: datetime, end: datetime) -> TimelineBlock:
    return TimelineBlock(
        start_time=start,
        end_time=end,
        timezone=start.tzname() or "",
        entries=["[TestApp] did something"],
        apps_used=["TestApp"],
        capture_count=1,
    )


@pytest.fixture
def seeded(ac_root) -> tuple[str, str, str]:
    """Seed one timeline block + two captures (one plaintext shot, one no shot).

    Returns ``(date, plain_stem, noshot_stem)``.
    """
    tz = datetime.now().astimezone().tzinfo
    day = datetime.now().astimezone().replace(
        hour=10, minute=0, second=0, microsecond=0, tzinfo=tz
    )
    start = day
    end = day + timedelta(minutes=1)
    plain_stem = _stem_for(day + timedelta(seconds=10))
    noshot_stem = _stem_for(day + timedelta(seconds=30))
    _write_capture(plain_stem, screenshot=_IMG_B64)
    _write_capture(noshot_stem, screenshot=None)

    with fts.cursor() as conn:
        timeline_store.ensure_schema(conn)
        timeline_store.insert(conn, _block(start, end))
        conn.commit()

    return day.strftime("%Y-%m-%d"), plain_stem, noshot_stem


# ─── /rewind/day ─────────────────────────────────────────────────────────────


def test_day_returns_blocks_with_capture_stems(seeded) -> None:
    date, plain_stem, noshot_stem = seeded
    client = _local_client(_rewind_cfg())
    resp = client.get(f"/rewind/day?date={date}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["date"] == date
    blocks = body["data"]["blocks"]
    assert len(blocks) == 1

    block = blocks[0]
    # carries the "what happened" fields
    for f in ("id", "start_time", "end_time", "entries", "apps_used", "captures"):
        assert f in block, f"missing field: {f}"

    stems = {c["stem"]: c["has_screenshot"] for c in block["captures"]}
    assert stems == {plain_stem: True, noshot_stem: False}
    # no image bytes inlined
    assert "image_base64" not in json.dumps(block)


def test_day_disabled_when_flag_off(seeded) -> None:
    """Explicit rewind_enabled=False → endpoint disabled → 404."""
    date, _plain, _noshot = seeded
    client = _local_client(_rewind_cfg(enabled=False))
    resp = client.get(f"/rewind/day?date={date}")
    assert resp.status_code == 404


def test_day_enabled_by_default(ac_root) -> None:
    """Default Config now has rewind_enabled=True → endpoint serves (not 404)."""
    client = _local_client(Config())
    resp = client.get("/rewind/day?date=2026-05-20")
    assert resp.status_code == 200


def test_day_empty_day_is_clean(ac_root) -> None:
    client = _local_client(_rewind_cfg())
    resp = client.get("/rewind/day?date=2030-01-01")
    assert resp.status_code == 200
    assert resp.json()["data"]["blocks"] == []


def test_day_malformed_date_is_404_not_500(ac_root) -> None:
    client = _local_client(_rewind_cfg())
    resp = client.get("/rewind/day?date=not-a-date")
    assert resp.status_code == 404


# ─── /rewind/screenshot ──────────────────────────────────────────────────────


def test_screenshot_plaintext_returns_bytes(seeded) -> None:
    _date, plain_stem, _noshot = seeded
    client = _local_client(_rewind_cfg())
    resp = client.get(f"/rewind/screenshot?stem={plain_stem}")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.content == _IMG_BYTES


def test_screenshot_encrypted_decrypts_with_env_key(ac_root, monkeypatch) -> None:
    """An encrypted capture + the env key set → endpoint decrypts and serves."""
    from persome import paths

    monkeypatch.setenv(screenshot_crypto.KEY_ENV, _HEX_KEY)
    key = screenshot_crypto.load_key()
    assert key is not None
    envelope = screenshot_crypto.encrypt(_IMG_B64, key)
    stem = _stem_for(
        datetime.now().astimezone().replace(hour=9, minute=0, second=5, microsecond=0)
    )
    (paths.capture_buffer_dir() / f"{stem}.json").write_text(
        json.dumps({"timestamp": stem, "screenshot": {"image_base64": envelope}}),
        encoding="utf-8",
    )

    client = _local_client(_rewind_cfg())
    resp = client.get(f"/rewind/screenshot?stem={stem}")
    assert resp.status_code == 200
    assert resp.content == _IMG_BYTES


def test_screenshot_encrypted_without_key_is_404(ac_root, monkeypatch) -> None:
    """Encrypted capture but no key in env → read_screenshot None → 404, no crash."""
    from persome import paths

    # Encrypt with a known key, then ensure the env has NO key at read time.
    monkeypatch.setenv(screenshot_crypto.KEY_ENV, _HEX_KEY)
    key = screenshot_crypto.load_key()
    assert key is not None
    envelope = screenshot_crypto.encrypt(_IMG_B64, key)
    monkeypatch.delenv(screenshot_crypto.KEY_ENV, raising=False)

    stem = _stem_for(
        datetime.now().astimezone().replace(hour=9, minute=1, second=5, microsecond=0)
    )
    (paths.capture_buffer_dir() / f"{stem}.json").write_text(
        json.dumps({"timestamp": stem, "screenshot": {"image_base64": envelope}}),
        encoding="utf-8",
    )

    client = _local_client(_rewind_cfg())
    resp = client.get(f"/rewind/screenshot?stem={stem}")
    assert resp.status_code == 404


def test_screenshot_missing_image_is_404(seeded) -> None:
    _date, _plain, noshot_stem = seeded
    client = _local_client(_rewind_cfg())
    resp = client.get(f"/rewind/screenshot?stem={noshot_stem}")
    assert resp.status_code == 404


def test_screenshot_unknown_stem_is_404(ac_root) -> None:
    client = _local_client(_rewind_cfg())
    resp = client.get("/rewind/screenshot?stem=2026-05-20T10-00-00p08-00")
    assert resp.status_code == 404


def test_screenshot_traversal_stem_is_404(ac_root) -> None:
    client = _local_client(_rewind_cfg())
    resp = client.get("/rewind/screenshot?stem=../../etc/passwd")
    assert resp.status_code == 404


def test_screenshot_disabled_by_default(seeded) -> None:
    _date, plain_stem, _noshot = seeded
    client = _local_client(_rewind_cfg(enabled=False))
    resp = client.get(f"/rewind/screenshot?stem={plain_stem}")
    assert resp.status_code == 404
