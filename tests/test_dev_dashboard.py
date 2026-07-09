"""The dev ops dashboard gate + the /attention/trajectory endpoint.

The dashboard is gated: 404 unless dev mode is on ([dev] enabled, set by the
Persome app for a dev-plan account, or PERSOME_DEV=1 locally). The attention endpoint
returns the by-dwell + chronological summary.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from persome.api import routes
from persome.store import fts
from persome.timeline import store as timeline_store


def test_dev_dashboard_404_when_off(ac_root: Path, monkeypatch) -> None:
    monkeypatch.delenv("PERSOME_DEV", raising=False)
    with pytest.raises(HTTPException) as ei:
        routes.dev_dashboard()
    assert ei.value.status_code == 404


def test_dev_dashboard_served_when_env_on(ac_root: Path, monkeypatch) -> None:
    monkeypatch.setenv("PERSOME_DEV", "1")
    resp = routes.dev_dashboard()
    body = resp.body.decode()
    assert "Persome" in body and "echarts" in body  # the page + chart lib
    assert "/events/stream" in body  # live SSE wired
    assert "/attention/trajectory" in body  # attention panel wired
    assert "运维" in body  # localized to Chinese
    # 原始捕获 (#raw) view surfaces OCR content + capture status (incl. WeChat,
    # whose text only comes via OCR). Lock the feature into the shipped page.
    assert 'href="#raw"' in body and "原始捕获" in body  # the raw-captures tab
    assert "OCR 识别内容" in body  # OCR text section
    assert "text_source" in body and "file_stem=" in body  # provenance + exact lookup


def test_dev_dashboard_file_override(ac_root: Path, monkeypatch) -> None:
    # A dev_dashboard.html dropped under the root is served verbatim (no rebuild).
    from persome import paths

    monkeypatch.setenv("PERSOME_DEV", "1")
    (paths.root() / "dev_dashboard.html").write_text(
        "<html>CUSTOM-OVERRIDE</html>", encoding="utf-8"
    )
    assert "CUSTOM-OVERRIDE" in routes.dev_dashboard().body.decode()


def test_dev_enabled_reads_config(ac_root: Path, monkeypatch) -> None:
    monkeypatch.delenv("PERSOME_DEV", raising=False)
    assert routes._dev_enabled() is False  # default [dev] enabled = false
    monkeypatch.setenv("PERSOME_DEV", "1")
    assert routes._dev_enabled() is True


def test_attention_trajectory_endpoint_shape(ac_root: Path) -> None:
    with fts.cursor() as conn:
        timeline_store.ensure_schema(conn)
    resp = routes.attention_trajectory()
    assert set(resp.data.keys()) >= {"by_dwell", "trajectory", "window"}
    assert isinstance(resp.data["by_dwell"], list)
