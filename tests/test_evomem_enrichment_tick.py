"""Entity/relation enrichment used by the shared model build."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import persome.evomem.person_graph as pg_mod
import persome.session.tick as tick
import persome.writer.attention_digest as digest_mod
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


def test_enrichment_forwards_case_evidence_cutoff(ac_root, monkeypatch) -> None:
    evidence_as_of = datetime(2026, 7, 13, 13, 46, tzinfo=UTC)
    seen: list[datetime | None] = []

    def _capture_case(cfg, *, evidence_as_of=None, **_kwargs):  # noqa: ANN001
        seen.append(evidence_as_of)
        return SimpleNamespace(written_count=0)

    monkeypatch.setattr(case_mod, "run_case_extraction", _capture_case)
    cfg = SimpleNamespace(person_graph_enabled=False, case_extraction_enabled=True)

    tick._run_evomem_enrichment_once(cfg, evidence_as_of=evidence_as_of)

    assert seen == [evidence_as_of]


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


def test_strict_enrichment_reports_failure_after_running_all_layers(ac_root, monkeypatch) -> None:
    _CALLS.clear()

    class _BoomGraph:
        def __init__(self, *_a, **_k) -> None:
            pass

        def ingest(self):  # noqa: ANN201
            _CALLS.append("person")
            raise RuntimeError("boom")

    monkeypatch.setattr(pg_mod, "PersonGraph", _BoomGraph)
    monkeypatch.setattr(case_mod, "run_case_extraction", _fake_case)
    cfg = SimpleNamespace(person_graph_enabled=True, case_extraction_enabled=True)
    with pytest.raises(RuntimeError, match="person_graph"):
        tick._run_evomem_enrichment_once(cfg, raise_on_error=True)
    assert _CALLS == ["person", "case"]


def test_enrichment_runs_attention_digest_when_enabled(ac_root, monkeypatch) -> None:
    _CALLS.clear()

    def _fake_digest(cfg, **_k):  # noqa: ANN001
        _CALLS.append("digest")
        return SimpleNamespace(committed=True, surfaces=["ProjA"])

    monkeypatch.setattr(pg_mod, "PersonGraph", _FakeGraph)
    monkeypatch.setattr(case_mod, "run_case_extraction", _fake_case)
    monkeypatch.setattr(digest_mod, "run_attention_digest", _fake_digest)
    cfg = SimpleNamespace(
        person_graph_enabled=True,
        case_extraction_enabled=True,
        attention_digest_enabled=True,
    )
    report = tick._run_evomem_enrichment_once(cfg)
    assert _CALLS == ["person", "case", "digest"]
    assert report["attention_digest"] == 1


def test_enrichment_skips_attention_digest_when_disabled(ac_root, monkeypatch) -> None:
    _CALLS.clear()

    def _boom_digest(*_a, **_k):
        raise AssertionError("attention digest should not run when disabled")

    monkeypatch.setattr(pg_mod, "PersonGraph", _FakeGraph)
    monkeypatch.setattr(case_mod, "run_case_extraction", _fake_case)
    monkeypatch.setattr(digest_mod, "run_attention_digest", _boom_digest)
    cfg = SimpleNamespace(
        person_graph_enabled=True,
        case_extraction_enabled=True,
        attention_digest_enabled=False,
    )
    report = tick._run_evomem_enrichment_once(cfg)
    assert _CALLS == ["person", "case"]
    assert report["attention_digest"] == 0


def test_enrichment_has_no_second_daemon_schedule() -> None:
    from persome.daemon import _build_task_registry

    assert "evomem-enrichment-tick" not in {t.name for t in _build_task_registry()}
