"""Periodic tick that builds closed timeline windows into TimelineBlocks.

Wall-clock-aligned so windows always line up at :00/:05/:10/... regardless
of when the tick fires. Idempotent via ``store.has_window`` — safe to
re-run or re-schedule. Runs as an asyncio task inside the daemon.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from ..capture import scheduler as capture_scheduler
from ..config import Config
from ..logger import get
from ..store import fts
from . import aggregator, store

logger = get("persome.timeline")

# How often to wake up and check for new closed windows. Slightly smaller
# than the window length so closed windows are picked up within one window
# of real time.
_TICK_INTERVAL_SECONDS = 60


def _now() -> datetime:
    return datetime.now().astimezone()


def _run_once(cfg: Config) -> int:
    window_minutes = cfg.timeline.window_minutes
    lookback_minutes = cfg.timeline.cold_lookback_minutes
    max_parallel = cfg.timeline.max_parallel_windows
    now = _now()
    current_floor = store.floor_to_window(now, window_minutes)

    with fts.cursor() as conn:
        latest_end = store.get_latest_end(conn)
    if latest_end is None:
        # First run — only build windows within the lookback horizon
        # so we don't LLM-process hours of backfill on startup.
        latest_end = current_floor - timedelta(minutes=lookback_minutes)

    pending = store.iter_windows(latest_end, current_floor, window_minutes)
    if not pending:
        return 0

    # Steady-state fast path: single window, zero overhead.
    if len(pending) == 1 or max_parallel <= 1:
        produced = 0
        for win_start, win_end in pending:
            block = aggregator.produce_block_for_window(cfg, start=win_start, end=win_end)
            if block is not None:
                produced += 1
        return produced

    # Backlog path: process up to max_parallel windows concurrently.
    produced = 0
    workers = min(max_parallel, len(pending))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(aggregator.produce_block_for_window, cfg, start=ws, end=we): (ws, we)
            for ws, we in pending
        }
        for fut in as_completed(futures):
            try:
                block = fut.result()
            except Exception as exc:  # noqa: BLE001
                ws, we = futures[fut]
                logger.warning(
                    "timeline: window %s→%s failed: %s", ws.isoformat(), we.isoformat(), exc
                )
            else:
                if block is not None:
                    produced += 1
    return produced


def _cleanup_buffer_once(cfg: Config) -> dict[str, int]:
    """One buffer-hygiene pass with the config-driven retention policy.

    Extracted from ``run_forever`` so the ``cfg`` → ``cleanup_buffer`` kwarg wiring is
    unit-testable — notably the #7 actionable extended-retention flags, which only take
    effect because they are forwarded here (the config fields are otherwise inert).
    """
    with fts.cursor() as conn:
        safe_end = store.get_latest_end(conn)
    return capture_scheduler.cleanup_buffer(
        cfg.capture.buffer_retention_hours,
        safe_end.isoformat() if safe_end else None,
        screenshot_retention_hours=cfg.capture.screenshot_retention_hours,
        screenshot_thumbnail_hours=cfg.capture.screenshot_thumbnail_hours,
        max_mb=cfg.capture.buffer_max_mb,
        # #7 (E5): keep the actionable subset's screenshots past the strip cutoff for
        # grounding/Rewind, when the user opts in. Flat top-level Config flags.
        extended_retention_enabled=cfg.capture_extended_retention_enabled,
        actionable_retention_days=cfg.capture_actionable_retention_days,
    )


async def run_forever(
    cfg: Config,
    on_blocks_produced: Callable[[int], Awaitable[None]] | None = None,
) -> None:
    """Daemon task: every minute, materialise any closed windows.

    ``on_blocks_produced`` (optional) is awaited each tick that materialises one
    or more new blocks. This is the block-flush trigger for the session-level
    intent recognizer — recognition fires exactly when fresh trajectory lands,
    instead of on a fixed timer.
    """
    logger.info(
        "timeline loop started (window=%d min, tick=%d s)",
        cfg.timeline.window_minutes,
        _TICK_INTERVAL_SECONDS,
    )
    while True:
        try:
            produced = await asyncio.to_thread(_run_once, cfg)
            if produced:
                logger.info("timeline: produced %d block(s) this tick", produced)
                if on_blocks_produced is not None:
                    try:
                        await on_blocks_produced(produced)
                    except Exception as exc:  # noqa: BLE001 - hook must not kill the loop
                        logger.error(
                            "timeline: on_blocks_produced hook failed: %s", exc, exc_info=True
                        )
            # Clean buffer files once the aggregator has absorbed them —
            # safe cutoff is the newest block's end_time.
            try:
                stats = await asyncio.to_thread(_cleanup_buffer_once, cfg)
                if any(stats.values()):
                    logger.info(
                        "timeline: buffer hygiene deleted=%d stripped=%d evicted=%d",
                        stats["deleted"],
                        stats["stripped"],
                        stats["evicted"],
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("timeline: buffer cleanup failed: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("timeline tick failed: %s", exc, exc_info=True)
        await asyncio.sleep(_TICK_INTERVAL_SECONDS)


def tick_now(cfg: Config) -> int:
    """Synchronous one-shot — for CLI debug. Returns blocks produced."""
    return _run_once(cfg)
