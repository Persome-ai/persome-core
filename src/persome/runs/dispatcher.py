"""run-dispatcher: the single asyncio task that pulls queued agent_runs and
executes them on a worker thread.

Event-driven: woken by enqueue via an asyncio.Event (NOT a busy poll — a busy
claim loop would contend on the SQLite write lock with an active run's
append_event firehose and trip 'database is locked'). A long fallback sleep
covers any missed wake. Per-kind concurrency caps replace dream's module Lock:
a second dream becomes a real queued row rather than a 409."""

from __future__ import annotations

import asyncio
import contextlib

from ..config import Config
from ..logger import get
from ..store import agent_runs as store
from ..store import fts
from .recorder import run_recorded

logger = get("persome.runs.dispatcher")

# Per-kind max simultaneous running rows. Replaces dream's module Lock.
CONCURRENCY: dict[str, int] = {
    "dream": 1,
    "bootstrap": 1,
    "summarize-week": 1,
    "evomem-compact-repair": 1,  # #526: compact 后 evo_nodes 自修，串行（幂等，多 tick 不堆积）
}
_DEFAULT_LIMIT = 1
_FALLBACK_SLEEP_S = 5.0

_inflight: dict[str, int] = {}

# Module-level wake event. On Python 3.11 asyncio.Event does not bind to a loop
# until it is first awaited, so creating it at import time is safe for both the
# daemon (one long-lived loop) and the test suite (each asyncio.run gets a fresh
# loop and the first await binds the event to it).
_wake: asyncio.Event = asyncio.Event()


def wake() -> None:
    """Signal the dispatcher that new work was enqueued.

    Safe to call from sync code even when no event loop is running — ``set()``
    on an unbound/idle Event never touches a loop, and the fallback sleep in
    ``run_dispatcher`` picks up the work regardless."""
    with contextlib.suppress(RuntimeError):
        _wake.set()


def _limit(kind: str) -> int:
    return CONCURRENCY.get(kind, _DEFAULT_LIMIT)


async def drain_once(cfg: Config) -> None:
    """Claim and dispatch as many queued runs as the per-kind limits allow.
    Each claimed run executes on a worker thread; we track in-flight in-process
    and decrement on completion."""
    for kind in list(CONCURRENCY.keys()):
        while _inflight.get(kind, 0) < _limit(kind):
            with fts.cursor() as conn:
                # DB-side running count + in-process count guard the limit.
                if store.count_inflight(conn, kind=kind) >= _limit(kind):
                    break
                run_id = store.claim_oldest_queued(conn, kind=kind)
            if run_id is None:
                break
            _inflight[kind] = _inflight.get(kind, 0) + 1

            def _done(_task: asyncio.Task[None], k: str = kind) -> None:
                _inflight[k] = max(0, _inflight.get(k, 0) - 1)
                wake()  # re-check the queue after one finishes

            task = asyncio.create_task(asyncio.to_thread(run_recorded, cfg, run_id))
            task.add_done_callback(_done)


async def run_dispatcher(cfg: Config) -> None:
    """Daemon task: drain on wake, with a fallback sleep so nothing is stranded."""
    logger.info("run-dispatcher started (concurrency=%s)", CONCURRENCY)
    # Pick up anything already queued at boot.
    wake()
    while True:
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(_wake.wait(), timeout=_FALLBACK_SLEEP_S)
            _wake.clear()
            await drain_once(cfg)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001  # pragma: no cover
            logger.exception("run-dispatcher loop error")
            await asyncio.sleep(1.0)
