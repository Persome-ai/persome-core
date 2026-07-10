"""Personal-model schema production is active in the Runtime."""

from __future__ import annotations

from persome.config import SchemaConfig


def test_schema_modeling_activated_by_default() -> None:
    cfg = SchemaConfig()
    assert cfg.enabled is True
    assert cfg.cross_domain_enabled is True
    assert cfg.root_synthesis_enabled is True
