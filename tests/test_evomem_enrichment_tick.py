"""#1/#2 scheduling — the daily evomem enrichment tick wiring.

`run_evomem_enrichment_tick` (registered as the `evomem-enrichment-tick` daemon task)
runs person-graph ingest (#1) + case extraction (#2) once per day. Both layers gate
internally on their own flags and are isolated in their own try. These tests pin the
forwarding + gating so flipping the flags actually makes the features RUN (before this
wiring they were enabled-but-inert).
"""

from __future__ import annotations

from types import SimpleNamespace

import persome.evomem.person_graph as pg_mod
import persome.session.tick as tick
import persome.writer.case_extractor as case_mod


class _FakeGraph:
    def __init__(self, *_a, **_k) -> None:
        pass

    def ingest(self) -> list[str]:
        _CALLS.append("person")
        return ["x"]


_CALLS: list[str] = []


def _fake_case(cfg, **_k):  # noqa: ANN001
    _CALLS.append("case")
    return SimpleNamespace(written_count=1)


def test_enrichment_runs_both_when_enabled(ac_root, monkeypatch) -> None:
    _CALLS.clear()
    monkeypatch.setattr(pg_mod, "PersonGraph", _FakeGraph)
    monkeypatch.setattr(case_mod, "run_case_extraction", _fake_case)
    cfg = SimpleNamespace(person_graph_enabled=True, case_extraction_enabled=True)
    tick._run_evomem_enrichment_once(cfg)
    assert _CALLS == ["person", "case"]


def test_enrichment_noop_when_both_disabled(ac_root, monkeypatch) -> None:
    _CALLS.clear()

    def _boom(*_a, **_k):  # PersonGraph must not even be constructed
        raise AssertionError("person graph should not run when disabled")

    monkeypatch.setattr(pg_mod, "PersonGraph", _boom)
    monkeypatch.setattr(case_mod, "run_case_extraction", _fake_case)
    cfg = SimpleNamespace(person_graph_enabled=False, case_extraction_enabled=False)
    tick._run_evomem_enrichment_once(cfg)
    assert _CALLS == []


def test_enrichment_person_failure_does_not_block_case(ac_root, monkeypatch) -> None:
    _CALLS.clear()

    class _BoomGraph:
        def __init__(self, *_a, **_k) -> None:
            pass

        def ingest(self):  # noqa: ANN201
            raise RuntimeError("boom")

    monkeypatch.setattr(pg_mod, "PersonGraph", _BoomGraph)
    monkeypatch.setattr(case_mod, "run_case_extraction", _fake_case)
    cfg = SimpleNamespace(person_graph_enabled=True, case_extraction_enabled=True)
    tick._run_evomem_enrichment_once(cfg)  # person boom is caught → case still runs
    assert _CALLS == ["case"]


def test_enrichment_task_registered_and_gated() -> None:
    from persome.daemon import _build_task_registry

    reg = {t.name: t for t in _build_task_registry()}
    assert "evomem-enrichment-tick" in reg
    task = reg["evomem-enrichment-tick"]
    one_on = SimpleNamespace(person_graph_enabled=True, case_extraction_enabled=False)
    both_off = SimpleNamespace(person_graph_enabled=False, case_extraction_enabled=False)
    assert task.enabled(one_on, False) is True
    assert task.enabled(both_off, False) is False
    assert task.enabled(one_on, True) is False  # disabled in --capture-only
