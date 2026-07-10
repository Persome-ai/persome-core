"""Session-count consolidation cadence (issue #49).

Verifies:
- `_check_and_trigger_consolidation` increments a persistent counter and
  fires the placeholder consolidation exactly at every Nth call.
- The counter survives SQLite re-open (i.e., "restart").
"""

from __future__ import annotations

from pathlib import Path

import pytest

from persome import config as config_mod
from persome.session import store as session_store
from persome.store import fts
from persome.writer import classifier as classifier_mod


def _counter() -> int:
    with fts.cursor() as conn:
        return int(
            session_store.get_system_state(conn, classifier_mod._COMPLETED_SESSION_COUNT_KEY, "0")
        )


def test_cadence_triggers_every_n_sessions(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """8 commits → 1 trigger; 16 commits → 2 triggers; 17 → still 2."""
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.writer.consolidation_cadence = 8

    calls: list[int] = []
    monkeypatch.setattr(
        classifier_mod,
        "_trigger_placeholder_consolidation",
        lambda _cfg: calls.append(1),
    )

    for _ in range(7):
        classifier_mod._check_and_trigger_consolidation(cfg)
    assert calls == []
    assert _counter() == 7

    classifier_mod._check_and_trigger_consolidation(cfg)  # 8th
    assert len(calls) == 1
    assert _counter() == 8

    # Sessions 9..15 — still 1 trigger.
    for _ in range(7):
        classifier_mod._check_and_trigger_consolidation(cfg)
    assert len(calls) == 1
    assert _counter() == 15

    classifier_mod._check_and_trigger_consolidation(cfg)  # 16th
    assert len(calls) == 2
    assert _counter() == 16


def test_counter_persists_across_reopen(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Counter lives in SQLite — closing the conn should not lose it."""
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.writer.consolidation_cadence = 8

    monkeypatch.setattr(
        classifier_mod,
        "_trigger_placeholder_consolidation",
        lambda _cfg: None,
    )

    for _ in range(3):
        classifier_mod._check_and_trigger_consolidation(cfg)
    assert _counter() == 3

    # Force a fresh connection by opening a new cursor — emulates restart.
    with fts.cursor() as conn:
        value = session_store.get_system_state(
            conn, classifier_mod._COMPLETED_SESSION_COUNT_KEY, "0"
        )
    assert value == "3"


def test_cadence_one_triggers_every_session(ac_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Edge case: cadence=1 means every commit triggers."""
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.writer.consolidation_cadence = 1

    calls: list[int] = []
    monkeypatch.setattr(
        classifier_mod,
        "_trigger_placeholder_consolidation",
        lambda _cfg: calls.append(1),
    )

    for _ in range(3):
        classifier_mod._check_and_trigger_consolidation(cfg)
    assert len(calls) == 3
    assert _counter() == 3
