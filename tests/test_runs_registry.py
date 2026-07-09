"""run_recorded + kind registry (Phase 1b)."""

from __future__ import annotations

from persome.config import load as load_config
from persome.runs import recorder, registry
from persome.store import agent_runs as store
from persome.store import fts


def test_registry_has_dream_and_bootstrap() -> None:
    assert "dream" in registry.KIND_REGISTRY
    assert "bootstrap" in registry.KIND_REGISTRY


def test_run_recorded_commits_and_tapes_events(ac_root, monkeypatch) -> None:
    cfg = load_config()

    # Fake executor: emits one progress event, returns a committed outcome.
    def fake_exec(cfg, on_event, payload):
        on_event("progress", {"value": 0.5, "label": "半程"})
        return registry.RunOutcome(committed=True, summary="ok", result_refs=[], iterations=2)

    monkeypatch.setitem(
        registry.KIND_REGISTRY,
        "dream",
        registry.KindSpec(kind="dream", title="每日整理", run=fake_exec),
    )
    with fts.cursor() as conn:
        rid = store.enqueue(conn, kind="dream", trigger="user", dispatch_source="user")
        store.mark_running(conn, rid)

    recorder.run_recorded(cfg, rid)

    with fts.cursor() as conn:
        run = store.get_run(conn, rid)
        evs = store.list_events(conn, rid)
    assert run.status == "committed"
    assert run.summary == "ok"
    assert any(e.type == "progress" for e in evs)
    assert any(e.type == "stage_end" for e in evs)


def test_run_recorded_fails_on_exception(ac_root, monkeypatch) -> None:
    cfg = load_config()

    def boom_exec(cfg, on_event, payload):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(
        registry.KIND_REGISTRY,
        "dream",
        registry.KindSpec(kind="dream", title="每日整理", run=boom_exec),
    )
    with fts.cursor() as conn:
        rid = store.enqueue(conn, kind="dream", trigger="user", dispatch_source="user")
        store.mark_running(conn, rid)

    recorder.run_recorded(cfg, rid)

    with fts.cursor() as conn:
        run = store.get_run(conn, rid)
    assert run.status == "failed"
    assert "kaboom" in run.error
