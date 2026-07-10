"""Dev-mode configuration and the attention trajectory endpoint."""

from __future__ import annotations

from pathlib import Path

from persome.api import routes
from persome.store import fts
from persome.timeline import store as timeline_store


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
