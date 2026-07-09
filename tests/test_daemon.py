"""Unit tests for daemon task registry and lifecycle (start / stop / health)."""

from __future__ import annotations

import asyncio
import errno
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from persome.config import (
    CaptureConfig,
    Config,
    DreamConfig,
    MCPConfig,
    SchemaConfig,
    SearchConfig,
)
from persome.daemon import (
    _SHUTDOWN_TIMEOUT_SECONDS,
    TaskDefinition,
    _build_task_registry,
    _create_tasks_from_registry,
    _mcp_loop,
    _on_task_done,
    _run,
    _shutdown_tasks,
)


def _enabled_names(cfg: Config, capture_only: bool = False) -> set[str]:
    return {td.name for td in _build_task_registry() if td.enabled(cfg, capture_only)}


async def _never() -> None:
    """Stand-in coroutine that never completes."""
    await asyncio.Event().wait()


def _base_cfg() -> Config:
    """Explicit baseline: dream/schema/ocr off, MCP on (streamable-http).

    The evomem enrichment layers (person-graph / case-extraction) default ON, so the
    minimal baseline turns them off explicitly to keep `evomem-enrichment-tick` out of
    the asserted default set (the on-by-default behavior is covered by
    `test_evomem_enrichment_tick`).
    """
    return Config(
        dream=DreamConfig(enabled=False),
        schema=SchemaConfig(enabled=False),
        capture=CaptureConfig(enable_ocr_fallback=False),
        mcp=MCPConfig(auto_start=True, transport="streamable-http"),
        search=SearchConfig(hybrid_enabled=False),
        person_graph_enabled=False,
        case_extraction_enabled=False,
    )


class TestRegistryEnabledPredicates:
    def test_default_full_mode(self) -> None:
        assert _enabled_names(_base_cfg()) == {
            "capture",
            "session",
            "daily-safety-net",
            "timeline",
            "flush",
            "classifier-tick",
            "run-dispatcher",
            "mcp",
        }

    def test_capture_only_disables_processing_tasks(self) -> None:
        assert _enabled_names(_base_cfg(), capture_only=True) == {
            "capture",
            "session",
            "daily-safety-net",
            "mcp",
        }

    def test_dream_tick_requires_flag_and_full_mode(self) -> None:
        cfg = Config(dream=DreamConfig(enabled=True))
        assert "dream-tick" in _enabled_names(cfg)
        assert "dream-tick" not in _enabled_names(cfg, capture_only=True)

    def test_schema_tick_requires_flag_and_full_mode(self) -> None:
        cfg = Config(schema=SchemaConfig(enabled=True))
        assert "schema-tick" in _enabled_names(cfg)
        assert "schema-tick" not in _enabled_names(cfg, capture_only=True)
        assert "schema-tick" not in _enabled_names(Config(schema=SchemaConfig(enabled=False)))

    def test_vector_embed_tick_requires_flag_and_full_mode(self) -> None:
        cfg = Config(search=SearchConfig(hybrid_enabled=True))
        assert "vector-embed-tick" in _enabled_names(cfg)
        assert "vector-embed-tick" not in _enabled_names(cfg, capture_only=True)
        assert "vector-embed-tick" not in _enabled_names(
            Config(search=SearchConfig(hybrid_enabled=False))
        )

    def test_intent_recognizer_rides_timeline_not_a_standalone_task(self) -> None:
        # Recognition now fires on the timeline task's block-flush hook, so there
        # is no separate "intent-recognizer-tick" task. The timeline task (which
        # carries it) is on in full mode and off in capture-only mode.
        assert "intent-recognizer-tick" not in _enabled_names(Config())
        assert "timeline" in _enabled_names(Config())
        assert "timeline" not in _enabled_names(Config(), capture_only=True)

    def test_mcp_disabled_when_auto_start_false(self) -> None:
        assert "mcp" not in _enabled_names(Config(mcp=MCPConfig(auto_start=False)))

    def test_mcp_disabled_for_stdio_transport(self) -> None:
        assert "mcp" not in _enabled_names(Config(mcp=MCPConfig(transport="stdio")))

    def test_mcp_enabled_for_sse_transport(self) -> None:
        assert "mcp" in _enabled_names(Config(mcp=MCPConfig(transport="sse")))

    def test_mcp_not_gated_by_capture_only(self) -> None:
        assert "mcp" in _enabled_names(Config(), capture_only=True)


class TestCreateTasksFromRegistry:
    async def test_task_names_match_enabled_registry(self) -> None:
        cfg = Config()
        registry = [replace(td, create=lambda c, sm: _never()) for td in _build_task_registry()]
        tasks = _create_tasks_from_registry(registry, cfg, MagicMock())
        try:
            assert {t.get_name() for t in tasks} == _enabled_names(cfg)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_capture_only_creates_fewer_tasks(self) -> None:
        cfg = Config()
        registry = [replace(td, create=lambda c, sm: _never()) for td in _build_task_registry()]
        tasks = _create_tasks_from_registry(registry, cfg, MagicMock(), capture_only=True)
        try:
            assert {t.get_name() for t in tasks} == _enabled_names(cfg, capture_only=True)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_done_callback_fires_on_task_exit(self) -> None:
        from unittest.mock import patch

        async def returns_immediately() -> None:
            return

        registry = [
            TaskDefinition(
                name="test-task",
                enabled=lambda cfg, capture_only: True,
                create=lambda c, sm: returns_immediately(),
            )
        ]
        with patch("persome.daemon._on_task_done") as mock_cb:
            tasks = _create_tasks_from_registry(registry, Config(), MagicMock())
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)  # let callbacks drain

        mock_cb.assert_called_once()
        assert mock_cb.call_args[0][0].get_name() == "test-task"


class TestOnTaskDone:
    """The done-callback turns silent task exits into daemon.log lines."""

    async def test_crash_logged_with_exception(self) -> None:
        async def boom() -> None:
            raise RuntimeError("kaboom")

        task = asyncio.create_task(boom(), name="boomer")
        await asyncio.gather(task, return_exceptions=True)
        with patch("persome.daemon.logger") as log:
            _on_task_done(task)
        log.error.assert_called_once()
        # exc_info carries the original exception for the traceback
        assert isinstance(log.error.call_args.kwargs["exc_info"], RuntimeError)

    async def test_clean_exit_logged_as_warning(self) -> None:
        async def returns() -> None:
            return

        task = asyncio.create_task(returns(), name="quitter")
        await asyncio.gather(task)
        with patch("persome.daemon.logger") as log:
            _on_task_done(task)
        log.warning.assert_called_once()
        log.error.assert_not_called()

    async def test_cancelled_task_is_ignored(self) -> None:
        task = asyncio.create_task(_never(), name="cancelled")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        with patch("persome.daemon.logger") as log:
            _on_task_done(task)
        log.error.assert_not_called()
        log.warning.assert_not_called()


class TestShutdownTasks:
    """``_shutdown_tasks`` cancels every task and warns about stragglers."""

    async def test_empty_list_is_noop(self) -> None:
        # Must not raise or call asyncio.wait on an empty set.
        await _shutdown_tasks([], timeout=1.0)

    async def test_cancels_and_drains_all_tasks(self) -> None:
        tasks = [asyncio.create_task(_never(), name=f"t{i}") for i in range(3)]
        await _shutdown_tasks(tasks, timeout=5.0)
        assert all(t.cancelled() for t in tasks)

    async def test_straggler_past_timeout_is_logged(self) -> None:
        async def wedged() -> None:
            # Swallow the cancellation so the task misses the drain deadline.
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(5.0)

        task = asyncio.create_task(wedged(), name="wedged")
        await asyncio.sleep(0)  # let it reach the await
        with patch("persome.daemon.logger") as log:
            await _shutdown_tasks([task], timeout=0.05)
        log.warning.assert_called_once()
        # Clean up the still-running straggler.
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class TestMcpLoop:
    """``_mcp_loop`` keeps the MCP server alive across crashes and binds."""

    async def test_clean_exit_returns_without_retry(self) -> None:
        async def run_async(cfg: Config) -> None:
            return

        with patch("persome.mcp.server.run_async", side_effect=run_async) as run:
            await _mcp_loop(Config())
        run.assert_awaited_once()

    async def test_eaddrinuse_retries_then_succeeds(self) -> None:
        calls = {"n": 0}

        async def run_async(cfg: Config) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(errno.EADDRINUSE, "address in use")
            return

        with (
            patch("persome.mcp.server.run_async", side_effect=run_async),
            patch("persome.daemon.asyncio.sleep") as sleep,
        ):
            await _mcp_loop(Config())
        # Slept once for the backoff before the successful second bind.
        sleep.assert_awaited_once()
        assert calls["n"] == 2

    async def test_non_eaddrinuse_oserror_returns_immediately(self) -> None:
        async def run_async(cfg: Config) -> None:
            raise OSError(errno.EACCES, "permission denied")

        with (
            patch("persome.mcp.server.run_async", side_effect=run_async),
            patch("persome.daemon.asyncio.sleep") as sleep,
            patch("persome.daemon.logger") as log,
        ):
            await _mcp_loop(Config())
        # Hard failure: log + return, no backoff sleep.
        log.error.assert_called_once()
        sleep.assert_not_awaited()

    async def test_generic_crash_retries_then_succeeds(self) -> None:
        calls = {"n": 0}

        async def run_async(cfg: Config) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient crash")
            return

        with (
            patch("persome.mcp.server.run_async", side_effect=run_async),
            patch("persome.daemon.asyncio.sleep") as sleep,
        ):
            await _mcp_loop(Config())
        sleep.assert_awaited_once()
        assert calls["n"] == 2

    async def test_cancellation_propagates(self) -> None:
        async def run_async(cfg: Config) -> None:
            raise asyncio.CancelledError

        with (
            patch("persome.mcp.server.run_async", side_effect=run_async),
            pytest.raises(asyncio.CancelledError),
        ):
            await _mcp_loop(Config())


class TestRunLifecycle:
    """Full ``_run``: pid file written on start, removed on stop, signal cuts loop."""

    def _no_task_cfg(self) -> Config:
        """Config where the registry yields no long-running tasks.

        capture_only drops the processing tasks; disabling auto_start drops
        mcp; capture+session+daily-safety-net are replaced with stubs by the
        test so the loop exits the moment the stop event fires.
        """
        return Config(mcp=MCPConfig(auto_start=False))

    async def test_writes_pid_and_cleans_up_on_signal(
        self, ac_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from persome import paths

        # Replace every registry task with a coroutine that never finishes, so
        # the daemon stays up until the stop event is set.
        stub_registry = [
            replace(td, create=lambda c, sm: _never()) for td in _build_task_registry()
        ]
        monkeypatch.setattr("persome.daemon._build_task_registry", lambda: stub_registry)

        async def driver() -> None:
            task = asyncio.create_task(_run(self._no_task_cfg(), capture_only=True))
            # Let _run reach the point where the pid file exists.
            for _ in range(50):
                await asyncio.sleep(0.01)
                if paths.pid_file().exists():
                    break
            assert paths.pid_file().exists()
            # Simulate SIGTERM by signalling the daemon's stop path: the
            # handler just sets an Event, so we cancel the run task which
            # exercises the same shutdown / cleanup branch deterministically.
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        await driver()

    async def test_clean_shutdown_removes_pid_file(
        self, ac_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from persome import paths

        # All tasks return immediately; FIRST_COMPLETED then unblocks _run and
        # it proceeds through the normal (non-cancelled) shutdown path.
        async def returns() -> None:
            return

        stub_registry = [
            replace(td, create=lambda c, sm: returns()) for td in _build_task_registry()
        ]
        monkeypatch.setattr("persome.daemon._build_task_registry", lambda: stub_registry)

        await _run(self._no_task_cfg(), capture_only=True)

        # Health invariant: a cleanly stopped daemon leaves no pid file behind.
        assert not paths.pid_file().exists()

    async def test_force_end_failure_does_not_crash_shutdown(
        self, ac_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def returns() -> None:
            return

        stub_registry = [
            replace(td, create=lambda c, sm: returns()) for td in _build_task_registry()
        ]
        monkeypatch.setattr("persome.daemon._build_task_registry", lambda: stub_registry)

        # SessionManager.force_end blowing up during shutdown must be swallowed
        # (it runs inside ``with suppress(Exception)``), so the pid file is
        # still cleaned up and _run returns normally.
        boom_manager = MagicMock()
        boom_manager.force_end.side_effect = RuntimeError("reducer thread gone")
        monkeypatch.setattr("persome.session.tick.build_manager", lambda cfg: boom_manager)

        from persome import paths

        await _run(self._no_task_cfg(), capture_only=True)

        boom_manager.force_end.assert_called_once()
        assert not paths.pid_file().exists()


def test_shutdown_timeout_constant_is_sane() -> None:
    # Guard against an accidental 0/negative that would make shutdown not wait.
    assert _SHUTDOWN_TIMEOUT_SECONDS > 0
