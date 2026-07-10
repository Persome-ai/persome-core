"""Top-level daemon: capture scheduler + timeline aggregator + session cutter.

The writer is driven by session boundaries. ``SessionManager.on_session_end``
(wired in ``session/tick.py``) spawns the S2 reducer on a daemon thread, then
the shared terminal finalizer runs classification, pattern detection, and
memory-delta modeling. A retry task recovers transient reducer failures.
"""

from __future__ import annotations

import asyncio
import os
import signal
import threading
from collections.abc import Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from . import paths
from .capture import ocr_local
from .capture import scheduler as capture_scheduler
from .config import Config
from .logger import get
from .session import tick as session_tick
from .timeline import tick as timeline_tick

logger = get("persome.daemon")

# How long shutdown will wait for cancelled tasks to drain before logging the
# stragglers and returning. A task wedged in its ``finally`` block can otherwise
# keep the daemon process alive and inert indefinitely (capture stopped, health
# "stale", pid still listed as running).
_SHUTDOWN_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class TaskDefinition:
    """Declares when and how to create one daemon task."""

    name: str
    enabled: Callable[[Config, bool], bool]
    create: Callable[[Config, Any], Coroutine[Any, Any, None]]


def _on_task_done(task: asyncio.Task) -> None:
    """Log any unexpected task exit so silent crashes surface in daemon.log."""
    if task.cancelled():
        return
    exc = task.exception()
    name = task.get_name()
    if exc is not None:
        logger.error("task %r crashed: %s", name, exc, exc_info=exc)
    else:
        logger.warning("task %r exited unexpectedly (no exception)", name)


async def _shutdown_tasks(tasks: list[asyncio.Task], *, timeout: float) -> None:
    """Cancel every task and wait up to ``timeout`` for them to drain.

    Any task that misses the deadline is named in the log so the next
    incident has a starting point.
    """
    if not tasks:
        return
    for t in tasks:
        t.cancel()
    _done, pending = await asyncio.wait(tasks, timeout=timeout)
    if pending:
        logger.warning(
            "shutdown: %d task(s) did not exit within %.1fs: %s",
            len(pending),
            timeout,
            sorted(t.get_name() for t in pending),
        )


async def _mcp_loop(cfg: Config) -> None:
    """Host the MCP server inside the daemon. On crash, back off and restart."""
    import errno as _errno

    from .mcp import server as mcp_server

    delay = 2.0
    while True:
        try:
            logger.info("mcp server starting (%s)", cfg.mcp.transport)
            await mcp_server.run_async(cfg)
            logger.info("mcp server exited cleanly")
            return
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            if exc.errno == _errno.EADDRINUSE:
                # Port still held by the previous daemon instance (TIME_WAIT or
                # slow shutdown). Retry with backoff — same as general crashes.
                logger.warning(
                    "mcp server failed to bind %s:%d — address in use, retrying in %.0fs",
                    cfg.mcp.host,
                    cfg.mcp.port,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
            else:
                logger.error(
                    "mcp server failed to bind %s:%d — %s",
                    cfg.mcp.host,
                    cfg.mcp.port,
                    exc,
                )
                return
        except Exception as exc:  # noqa: BLE001
            logger.warning("mcp server crashed: %s (restarting in %.0fs)", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)


def _build_task_registry() -> list[TaskDefinition]:
    """Return the complete set of daemon task definitions."""
    return [
        TaskDefinition(
            name="capture",
            enabled=lambda cfg, capture_only: True,
            create=lambda cfg, sm: capture_scheduler.run_forever(
                cfg.capture,
                pre_capture_hook=sm.on_event,
            ),
        ),
        TaskDefinition(
            name="session",
            enabled=lambda cfg, capture_only: True,
            create=lambda cfg, sm: session_tick.run_check_cuts(cfg, sm),
        ),
        TaskDefinition(
            name="reducer-retry",
            enabled=lambda cfg, capture_only: cfg.reducer.enabled,
            create=lambda cfg, sm: session_tick.run_reducer_retry_tick(cfg),
        ),
        TaskDefinition(
            name="daily-safety-net",
            enabled=lambda cfg, capture_only: True,
            create=lambda cfg, sm: session_tick.run_daily_safety_net(cfg, sm),
        ),
        TaskDefinition(
            name="timeline",
            enabled=lambda cfg, capture_only: not capture_only,
            create=lambda cfg, sm: timeline_tick.run_forever(cfg),
        ),
        TaskDefinition(
            name="flush",
            enabled=lambda cfg, capture_only: not capture_only,
            create=lambda cfg, sm: session_tick.run_flush_tick(cfg, sm),
        ),
        TaskDefinition(
            name="classifier-tick",
            enabled=lambda cfg, capture_only: (
                not capture_only and not cfg.memory_delta.apply_enabled
            ),
            create=lambda cfg, sm: session_tick.run_classifier_tick(cfg, sm),
        ),
        TaskDefinition(
            name="vector-embed-tick",
            enabled=lambda cfg, capture_only: not capture_only and cfg.search.hybrid_enabled,
            create=lambda cfg, sm: session_tick.run_vector_embed_tick(cfg),
        ),
        TaskDefinition(
            name="schema-tick",
            enabled=lambda cfg, capture_only: not capture_only and cfg.schema.enabled,
            create=lambda cfg, sm: session_tick.run_schema_tick(cfg),
        ),
        TaskDefinition(
            name="mcp",
            enabled=lambda cfg, capture_only: (
                cfg.mcp.auto_start and cfg.mcp.transport in ("sse", "streamable-http")
            ),
            create=lambda cfg, sm: _mcp_loop(cfg),
        ),
    ]


def _create_tasks_from_registry(
    registry: list[TaskDefinition],
    cfg: Config,
    session_manager: Any,
    *,
    capture_only: bool = False,
) -> list[asyncio.Task]:
    """Create and register all enabled tasks from the registry."""
    tasks = []
    for task_def in registry:
        if task_def.enabled(cfg, capture_only):
            task: asyncio.Task[None] = asyncio.create_task(
                task_def.create(cfg, session_manager),
                name=task_def.name,
            )
            task.add_done_callback(_on_task_done)
            tasks.append(task)
    return tasks


async def _run(cfg: Config, *, capture_only: bool = False, hard_exit: bool = False) -> None:
    paths.ensure_dirs()
    paths.pid_file().write_text(str(os.getpid()))

    # Register this daemon ("Persome Backend") in the macOS Screen Recording list + prompt,
    # so screenshots capture real app windows instead of just the desktop wallpaper (and
    # so OCR can grab AX-poor apps). Idempotent; a launchd background process won't get a
    # modal prompt, but the binary now appears in System Settings → Privacy & Security →
    # Screen Recording for the user to enable, then restart Persome. Never blocks boot.
    from .capture import screen_recording

    screen_recording.request_screen_recording()

    # evomem survivability base (SSOT switch design §3.3): chain-invariant
    # self-check at startup. Gated on [evomem] integrity_check_enabled (the
    # hook itself no-ops when off); alert-only unless freeze_writes_on_failure
    # is also set. Never raises — a failed check alerts, it doesn't block boot.
    from .evomem import integrity as evo_integrity

    await asyncio.to_thread(evo_integrity.startup_check, cfg)

    # (OCR warmup is started AFTER the signal handlers below, because importing
    # PaddleOCR/paddle hijacks the fatal signals — see _install_signal_handlers.)

    # Production hybrid retrieval: one shared wiring for the FULL read path
    # (fts.wire_read_path — hybrid gates, pool weights, tags/recency, vectors
    # write gate). Dense activates ONLY when hybrid is on AND an embeddings
    # endpoint is configured (``embeddings_client.available()``) so the
    # default-ON ship stays SAFE without creds: byte-identical BM25, no
    # vector_queue growth. #557 design principle: MCP-side callers get the
    # 满血版 memory — the SAME function runs in MCP ``build_server``, so the
    # standalone stdio server and the in-daemon HTTP server serve the same
    # full-power read stack as the daemon itself.
    from .store import fts as fts_hybrid

    fts_hybrid.wire_read_path(cfg)

    # SessionManager observes every capture-worthy event and fires the
    # reducer via its on_session_end callback. Built even when
    # capture_only is true so session rows still land on disk.
    session_manager = session_tick.build_manager(cfg)
    registry = _build_task_registry()
    tasks = _create_tasks_from_registry(registry, cfg, session_manager, capture_only=capture_only)

    stop = asyncio.Event()

    def _handle_stop() -> None:
        logger.info("shutdown signal received")
        stop.set()

    def _hard_stop() -> None:
        # Real-daemon SIGTERM/SIGINT path (launchd bootout / app quit). Exit the
        # process *immediately* via os._exit (no signal re-raise, no Python
        # teardown). os._exit is the only clean way out here: the bundled
        # PaddlePaddle (on-device OCR) installs a glog FailureSignalHandler at
        # import that hijacks the fatal signals (SIGTERM/SIGSEGV/…) — left in
        # place it intercepts our SIGTERM, dumps a stack and re-raises, which
        # macOS records as a SIGSEGV crash report on *every* quit (the long-blamed
        # "teardown SIGSEGV"). _install_signal_handlers() re-claims the signals
        # after the OCR warmup so we get here at all; doing os._exit (not a signal)
        # then exits without tripping glog. The pidfile is the one cheap durable
        # bit; the SQLite WAL is crash-safe and the boot safety-net force-ends the
        # session left open, so we accept losing the graceful force_end here.
        with suppress(Exception):
            paths.pid_file().unlink()
        os._exit(0)

    loop = asyncio.get_running_loop()
    _on_signal = _hard_stop if hard_exit else _handle_stop

    def _install_signal_handlers() -> None:
        # asyncio.add_signal_handler calls signal.signal under the hood, so a
        # second install REPLACES whatever is currently registered — including
        # paddle/glog's FailureSignalHandler. Must run on the loop (main) thread.
        for sig in (signal.SIGTERM, signal.SIGINT):
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, _on_signal)

    _install_signal_handlers()

    # Warm the local OCR engine off-thread (so the first AX-poor capture doesn't
    # pay the one-time graph-load latency) — but importing PaddleOCR/paddle
    # installs glog's FailureSignalHandler over ours, so re-claim the signals on
    # the loop thread once warmup finishes. Pure side channel; never blocks boot.
    if cfg.capture.enable_ocr_fallback:

        def _warm_ocr() -> None:
            try:
                ocr_local.warm(cfg.capture.ocr_tier)
            except Exception as exc:  # noqa: BLE001
                logger.warning("boot: OCR warmup failed: %s", exc)
            finally:
                # Re-take SIGTERM/SIGINT from paddle/glog (see _hard_stop).
                with suppress(RuntimeError):
                    loop.call_soon_threadsafe(_install_signal_handlers)

        threading.Thread(target=_warm_ocr, name="ocr-warm", daemon=True).start()

    done_task = asyncio.create_task(stop.wait())
    await asyncio.wait([done_task, *tasks], return_when=asyncio.FIRST_COMPLETED)

    if hard_exit:
        # Real daemon process (launchd/app `start --foreground`). Do ONLY the
        # fast, durable shutdown work, then hard-exit — deliberately skip the
        # async task drain (_shutdown_tasks).
        #
        # The drain runs Python — spinning the event loop for up to
        # _SHUTDOWN_TIMEOUT_SECONDS awaiting task cancellation — while background LLM modeling
        # and te3-large embedding calls are still mid-flight on the default
        # executor (asyncio.to_thread), parked in a TLS read (_ssl__SSLSocket_read).
        # On a real app quit there are typically several such SSL workers live;
        # any Python/loop activity in that window can free OpenSSL state out from
        # under them and SIGSEGV — an intermittent crash report on roughly half of
        # app quits, observed even AFTER the loop-wedge fix because the crash
        # happens here, BEFORE `_run` returns (so run()'s os._exit never reached).
        #
        # force_end + pidfile unlink are a fast DB write + a syscall; os._exit
        # then kills the abandoned tasks and their SSL worker threads with the
        # process. SQLite WAL is crash-safe, the boot safety-net force-ends any
        # session still open. This collapses the SSL-live window to ~one DB write.
        with suppress(Exception):
            session_manager.force_end(reason="daemon-shutdown")
        with suppress(FileNotFoundError):
            paths.pid_file().unlink()
        logger.info("daemon stopped")
        os._exit(0)

    # Soft path (tests / embedded callers drive `_run` directly and expect it to
    # return): full graceful drain, no hard-exit.
    await _shutdown_tasks(tasks, timeout=_SHUTDOWN_TIMEOUT_SECONDS)

    # Flush the currently open session so its S2 reducer has a chance
    # to run. The daemon-thread reducer spawned by the callback will be
    # killed when the process exits, but a row with status='ended'
    # survives and the next boot's safety-net picks it up.
    with suppress(Exception):
        session_manager.force_end(reason="daemon-shutdown")

    with suppress(FileNotFoundError):
        paths.pid_file().unlink()
    logger.info("daemon stopped")


def run(cfg: Config, *, capture_only: bool = False) -> None:
    """Boot the daemon event loop, block until a stop signal, then hard-exit.

    Two cooperating pieces stop the app-quit / launchd-bootout teardown SIGSEGV,
    whose root is the daemon's background LLM-modeling / te3-large embedding
    calls: every off-loop blocking call goes through ``asyncio.to_thread`` → the
    default executor, and at quit time several of those workers are parked in a
    TLS read (``_ssl__SSLSocket_read``). Running *any* event-loop / interpreter
    teardown while they are live can free OpenSSL state out from under them and
    SIGSEGV (and, before this, hang).

    1. ``_run(..., hard_exit=True)`` does ONLY the fast durable shutdown
       (force-end the session, unlink the pidfile) and then ``os._exit(0)`` — it
       SKIPS the async task drain so almost no Python runs while
       the SSL workers are live. (The crash used to fire *inside* that skipped
       window, before ``_run`` returned, which is why a trailing ``os._exit`` here
       alone was not enough.)
    2. We also drive the loop manually instead of ``asyncio.run`` so that, on any
       path that does fall through, we never reach ``asyncio.run``'s
       ``loop.shutdown_default_executor()`` (which *joins* those SSL-blocked
       workers and wedges shutdown forever).

    ``_run`` stays a pure coroutine that returns normally without ``hard_exit``
    (tests drive it directly), so the process-killing behaviour lives only on this
    entrypoint wrapper. The trailing ``os._exit`` is a backstop for the soft path.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run(cfg, capture_only=capture_only, hard_exit=True))
    except KeyboardInterrupt:
        # Foreground Ctrl+C when the loop signal handler wasn't installed; _run's
        # graceful path may not have run, but the boot safety-net recovers the open
        # session. Exit clean rather than dumping a traceback.
        pass
    except BaseException:  # noqa: BLE001 — log, then hard-exit non-zero; never fall into the racy teardown
        logger.exception("daemon crashed")
        os._exit(1)
    os._exit(0)
