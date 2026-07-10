"""POST /captures/ingest — Swift-side capture push into the daemon pipeline.

The Swift "Persome" process now owns OS capture (AX tree + screenshot); the daemon
becomes a zero-permission compute backend that receives pre-captured payloads via
this endpoint and runs the SAME enrich → persist → hook tail as the in-daemon
capture loop. These tests pin the contract:

  * the posted AX tree is re-enriched BYTE-COMPATIBLY with the daemon's own
    capture path (focused_element / visible_text / url),
  * the capture lands in the buffer and is readable through the production
    capture reader used by MCP,
  * a live capture runner updates session activity and content-dedups identical
    consecutive pushes.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from persome import paths
from persome.api import build_api_app
from persome.config import load as load_config


def _client(cfg=None):
    if cfg is None:
        cfg = load_config()
    return TestClient(build_api_app(cfg))


def _payload(*, with_screenshot: bool = False) -> tuple[dict, dict]:
    cap = {
        "timestamp": "2026-07-10T09:00:00+08:00",
        "trigger": {"event_type": "AXFocusedWindowChanged"},
        "window_meta": {
            "app_name": "Synthetic Browser",
            "title": "Runtime status",
            "bundle_id": "com.google.Chrome",
        },
        "ax_tree": {
            "apps": [
                {
                    "name": "Synthetic Browser",
                    "bundle_id": "com.google.Chrome",
                    "is_frontmost": True,
                    "windows": [
                        {
                            "title": "Runtime status",
                            "focused": True,
                            "elements": [
                                {
                                    "role": "AXTextField",
                                    "value": "https://example.com/runtime",
                                },
                                {
                                    "role": "AXWebArea",
                                    "title": "Runtime status",
                                    "children": [
                                        {
                                            "role": "AXStaticText",
                                            "value": "The local model is ready.",
                                        }
                                    ],
                                },
                            ],
                        }
                    ],
                }
            ]
        },
        "ax_metadata": {"synthetic": True},
        "screenshot": {
            "image_base64": "aGVsbG8=",
            "mime_type": "image/jpeg",
            "width": 1,
            "height": 1,
        },
    }
    payload = {
        "timestamp": cap["timestamp"],
        "trigger": cap.get("trigger") or {"event_type": "AXFocusedWindowChanged"},
        "window_meta": cap["window_meta"],
        "ax_tree": cap["ax_tree"],
        "ax_metadata": cap.get("ax_metadata", {}),
    }
    if with_screenshot:
        payload["screenshot"] = cap.get("screenshot")
    return cap, payload


def test_ingest_writes_and_enriches_byte_compatibly(ac_root) -> None:
    from persome.capture import s1_parser

    cap, payload = _payload()
    resp = _client().post("/captures/ingest", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data.get("id")

    files = list(paths.capture_buffer_dir().glob("*.json"))
    assert len(files) == 1
    out = json.loads(files[0].read_text())

    # The ingest path must reproduce the DAEMON's own enrichment for identical
    # inputs (both paths share `_finalize_capture` → `s1_parser.enrich`). Compute
    # the reference from the same AX tree rather than the stored fixture, which is
    # a snapshot that can drift as the renderer evolves.
    ref = {"ax_tree": cap["ax_tree"], "window_meta": cap["window_meta"]}
    s1_parser.enrich(ref)
    assert out["window_meta"] == cap["window_meta"]
    assert out["visible_text"] == ref["visible_text"]
    assert out["focused_element"] == ref["focused_element"]
    assert out["url"] == ref["url"]
    assert out["capture_source"] == "ingest"


def test_ingest_readable_via_recent(ac_root) -> None:
    from persome.mcp.captures import read_recent_capture

    _, payload = _payload()
    client = _client()
    assert client.post("/captures/ingest", json=payload).status_code == 200
    rec = read_recent_capture()
    assert rec is not None
    assert rec["app_name"] == payload["window_meta"]["app_name"]


def test_ingest_through_runner_fires_session_hook_and_dedups(ac_root) -> None:
    from persome.capture import scheduler

    seen: list[dict] = []
    runner = scheduler._CaptureRunner(
        load_config().capture,
        provider=None,  # ingest never builds via the provider
        pre_capture_hook=lambda trigger: seen.append(trigger),
    )
    scheduler._set_active_runner(runner)
    try:
        _, payload = _payload()
        client = _client()
        first = client.post("/captures/ingest", json=payload).json()["data"]
        second = client.post("/captures/ingest", json=payload).json()["data"]
        # Identical content → second push is a no-op (content fingerprint dedup).
        assert first.get("id")
        assert second.get("deduped") is True
        assert len(list(paths.capture_buffer_dir().glob("*.json"))) == 1
        # The session hook fires once; a duplicate does not refresh activity.
        assert len(seen) == 1
        assert seen[0]["event_type"] == "AXFocusedWindowChanged"
    finally:
        scheduler._set_active_runner(None)


# ── Hardening (codex adversarial review findings) ────────────────────────────


def test_ingest_rejects_path_traversal_timestamp(ac_root) -> None:
    """An untrusted timestamp must not escape the capture buffer or clobber files.

    The payload timestamp flows into the capture filename; a non-ISO8601 value (here a
    path-traversal string) is replaced with a safe server timestamp, so the file lands
    INSIDE the buffer with a separator-free stem.
    """
    _, payload = _payload()
    payload["timestamp"] = "../../../etc/evil"
    resp = _client().post("/captures/ingest", json=payload)
    assert resp.status_code == 200, resp.text

    files = list(paths.capture_buffer_dir().glob("*.json"))
    assert len(files) == 1  # nothing escaped the buffer
    assert files[0].parent == paths.capture_buffer_dir()
    assert "/" not in files[0].stem
    out = json.loads(files[0].read_text())
    datetime.fromisoformat(out["timestamp"])  # replaced with a valid ISO8601 stamp


def test_ingest_honors_screenshot_optout(ac_root) -> None:
    """With [capture].include_screenshot=false the daemon must not persist a pushed image."""
    from persome.api import routes as routes_mod

    routes_mod.set_config(None)  # force _get_cfg() → load_config() (reads our config.toml)
    (ac_root / "config.toml").write_text("[capture]\ninclude_screenshot = false\n")
    try:
        _, payload = _payload(with_screenshot=True)
        assert payload.get("screenshot")  # the fixture really carries a screenshot
        resp = _client().post("/captures/ingest", json=payload)
        assert resp.status_code == 200, resp.text
        out = json.loads(next(paths.capture_buffer_dir().glob("*.json")).read_text())
        assert "screenshot" not in out
    finally:
        routes_mod.set_config(None)


def test_commit_prebuilt_propagates_write_error_not_dedup(ac_root, monkeypatch) -> None:
    """A real write failure must PROPAGATE (→ HTTP 500), never be reported as a dedup."""
    from persome.capture import scheduler

    runner = scheduler._CaptureRunner(load_config().capture, provider=None)

    def boom(_out):
        raise OSError("disk full")

    monkeypatch.setattr(scheduler, "_write_capture", boom)
    out = scheduler.build_ingest_capture(load_config().capture, _payload()[1])
    assert out is not None
    with pytest.raises(OSError):
        runner.commit_prebuilt(out)
