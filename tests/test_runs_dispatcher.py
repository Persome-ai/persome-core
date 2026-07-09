"""run-dispatcher loop (Phase 1b)."""

from __future__ import annotations

import asyncio

from persome.config import load as load_config
from persome.runs import dispatcher, registry
from persome.store import agent_runs as store
from persome.store import fts


def test_dispatcher_drains_queue_once(ac_root, monkeypatch) -> None:
    cfg = load_config()

    def fake_exec(cfg, on_event, payload):
        return registry.RunOutcome(committed=True, summary="ok")

    monkeypatch.setitem(
        registry.KIND_REGISTRY,
        "dream",
        registry.KindSpec(kind="dream", title="t", run=fake_exec),
    )
    with fts.cursor() as conn:
        rid = store.enqueue(conn, kind="dream", trigger="user", dispatch_source="user")

    async def drive():
        # one pass of the claim/dispatch step, then let the to_thread finish
        await dispatcher.drain_once(cfg)
        for _ in range(50):
            with fts.cursor() as conn:
                if store.get_run(conn, rid).status == "committed":
                    break
            await asyncio.sleep(0.05)

    asyncio.run(drive())
    with fts.cursor() as conn:
        assert store.get_run(conn, rid).status == "committed"


def test_dispatcher_respects_per_kind_limit(ac_root, monkeypatch) -> None:
    cfg = load_config()
    monkeypatch.setattr(dispatcher, "CONCURRENCY", {"dream": 1})
    with fts.cursor() as conn:
        # simulate one already running (enqueue + immediately mark running)
        running = store.enqueue(conn, kind="dream", trigger="user", dispatch_source="user")
        store.mark_running(conn, running)
        # now enqueue a second dream (first is no longer queued, so this creates a new queued row)
        store.enqueue(conn, kind="dream", trigger="user", dispatch_source="user")

    async def drive():
        await dispatcher.drain_once(cfg)

    asyncio.run(drive())
    # the queued one must stay queued (limit 1, one already running)
    with fts.cursor() as conn:
        queued = [
            r
            for r in store.list_runs_in_window(
                conn,
                start=__import__("datetime")
                .datetime.now()
                .astimezone()
                .replace(hour=0, minute=0, second=0, microsecond=0),
                end=__import__("datetime")
                .datetime.now()
                .astimezone()
                .replace(hour=23, minute=59, second=59),
                statuses=["queued"],
            )
        ]
    assert len(queued) == 1
