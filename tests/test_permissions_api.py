"""Tests for the GET /permissions endpoint and the ax_trusted() probe.

The daemon is the process that reads the AX tree (via mac-ax-helper /
mac-ax-watcher), so /permissions reports the daemon's own Accessibility trust —
the authoritative signal the app's onboarding reflects instead of self-checking
in the GUI process (which would create a redundant second TCC principal).
"""

from __future__ import annotations

import platform

from fastapi.testclient import TestClient

from persome.api import build_api_app
from persome.capture import ax_capture


def _make_client() -> TestClient:
    return TestClient(build_api_app())


def test_permissions_reports_granted(monkeypatch) -> None:
    monkeypatch.setattr(ax_capture, "ax_trusted", lambda: True)
    resp = _make_client().get("/permissions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["accessibility"] == "granted"


def test_permissions_reports_denied(monkeypatch) -> None:
    monkeypatch.setattr(ax_capture, "ax_trusted", lambda: False)
    resp = _make_client().get("/permissions")
    assert resp.status_code == 200
    assert resp.json()["data"]["accessibility"] == "denied"


def test_ax_trusted_false_off_darwin() -> None:
    """On non-macOS hosts (the Linux CI gate) the probe is a safe False — no
    framework load, no crash."""
    if platform.system() == "Darwin":
        # On a real mac it returns a real bool; just assert the type/no-throw.
        assert isinstance(ax_capture.ax_trusted(), bool)
    else:
        assert ax_capture.ax_trusted() is False
