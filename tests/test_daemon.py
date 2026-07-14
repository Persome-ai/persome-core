"""Unit tests for daemon task registry and lifecycle (start / stop / health)."""

from __future__ import annotations

import asyncio
import errno
import json
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from persome.config import (
    CaptureConfig,
    Config,
    MCPConfig,
    ReducerConfig,
    SchemaConfig,
    SearchConfig,
)
from persome.daemon import (
    _SHUTDOWN_TIMEOUT_SECONDS,
    TaskDefinition,
    _build_task_registry,
    _create_tasks_from_registry,
    _hard_exit,
    _mcp_loop,
    _on_task_done,
    _run,
    _shutdown_tasks,
    _sync_human_after_update_commit,
)


def _enabled_names(cfg: Config, capture_only: bool = False) -> set[str]:
    return {td.name for td in _build_task_registry() if td.enabled(cfg, capture_only)}


async def _never() -> None:
    """Stand-in coroutine that never completes."""
    await asyncio.Event().wait()


def _base_cfg() -> Config:
    """Explicit baseline: schema/ocr off, MCP on (streamable-http)."""
    return Config(
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
            "reducer-retry",
            "daily-safety-net",
            "timeline",
            "flush",
            "mcp",
        }

    def test_capture_only_disables_processing_tasks(self) -> None:
        assert _enabled_names(_base_cfg(), capture_only=True) == {
            "capture",
            "session",
            "reducer-retry",
            "daily-safety-net",
            "mcp",
        }

    def test_schema_tick_requires_flag_and_full_mode(self) -> None:
        cfg = Config(schema=SchemaConfig(enabled=True))
        assert "schema-tick" in _enabled_names(cfg)
        assert "model-refresh" in _enabled_names(cfg)
        assert "schema-tick" not in _enabled_names(cfg, capture_only=True)
        assert "model-refresh" not in _enabled_names(cfg, capture_only=True)
        assert "schema-tick" not in _enabled_names(Config(schema=SchemaConfig(enabled=False)))
        assert "model-refresh" not in _enabled_names(Config(schema=SchemaConfig(enabled=False)))

    def test_vector_embed_tick_requires_flag_and_full_mode(self) -> None:
        cfg = Config(search=SearchConfig(hybrid_enabled=True))
        assert "vector-embed-tick" in _enabled_names(cfg)
        assert "vector-embed-tick" not in _enabled_names(cfg, capture_only=True)
        assert "vector-embed-tick" not in _enabled_names(
            Config(search=SearchConfig(hybrid_enabled=False))
        )

    def test_classifier_tick_only_runs_for_legacy_classifier_mode(self) -> None:
        cfg = _base_cfg()
        assert "classifier-tick" not in _enabled_names(cfg)
        cfg.memory_delta.apply_enabled = False
        assert "classifier-tick" in _enabled_names(cfg)

    def test_mcp_disabled_when_auto_start_false(self) -> None:
        assert "mcp" not in _enabled_names(Config(mcp=MCPConfig(auto_start=False)))

    def test_mcp_disabled_for_stdio_transport(self) -> None:
        assert "mcp" not in _enabled_names(Config(mcp=MCPConfig(transport="stdio")))

    def test_mcp_enabled_for_sse_transport(self) -> None:
        assert "mcp" in _enabled_names(Config(mcp=MCPConfig(transport="sse")))

    def test_mcp_not_gated_by_capture_only(self) -> None:
        assert "mcp" in _enabled_names(Config(), capture_only=True)

    def test_reducer_retry_requires_reducer(self) -> None:
        assert "reducer-retry" in _enabled_names(Config(), capture_only=True)
        assert "reducer-retry" not in _enabled_names(
            Config(reducer=ReducerConfig(enabled=False)), capture_only=True
        )


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

    async def test_uvicorn_systemexit_wrapping_eaddrinuse_retries(self) -> None:
        # uvicorn converts bind failures into SystemExit(3); letting it escape
        # took down the whole daemon and left launchd crash-looping into the
        # still-bound port. The loop must unwrap it and back off like a plain
        # EADDRINUSE.
        calls = {"n": 0}

        async def run_async(cfg: Config) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                try:
                    raise OSError(errno.EADDRINUSE, "address in use")
                except OSError:
                    # mirrors uvicorn's sys.exit(STARTUP_FAILURE): __context__
                    # carries the bind error even with display suppressed
                    raise SystemExit(3) from None
            return

        with (
            patch("persome.mcp.server.run_async", side_effect=run_async),
            patch("persome.daemon.asyncio.sleep") as sleep,
        ):
            await _mcp_loop(Config())
        sleep.assert_awaited_once()
        assert calls["n"] == 2

    async def test_uvicorn_systemexit_other_bind_error_returns_immediately(self) -> None:
        async def run_async(cfg: Config) -> None:
            try:
                raise PermissionError(1, "operation not permitted")
            except PermissionError:
                raise SystemExit(3) from None

        with (
            patch("persome.mcp.server.run_async", side_effect=run_async),
            patch("persome.daemon.asyncio.sleep") as sleep,
            patch("persome.daemon.logger") as log,
        ):
            await _mcp_loop(Config())
        # Hard failure: log + return with the daemon still alive, no backoff.
        log.error.assert_called_once()
        sleep.assert_not_awaited()

    @pytest.mark.parametrize("code", [0, 3])
    async def test_non_uvicorn_systemexit_propagates(self, code: int) -> None:
        async def run_async(cfg: Config) -> None:
            raise SystemExit(code)

        with (
            patch("persome.mcp.server.run_async", side_effect=run_async),
            pytest.raises(SystemExit) as raised,
        ):
            await _mcp_loop(Config())
        assert raised.value.code == code

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
        monkeypatch.setattr(
            "persome.daemon._build_task_registry", lambda _receipt=None: stub_registry
        )

        async def driver() -> None:
            task = asyncio.create_task(_run(self._no_task_cfg(), capture_only=True))
            # Let _run reach the point where the pid file exists.
            for _ in range(50):
                await asyncio.sleep(0.01)
                if paths.pid_file().exists() and paths.runtime_state_file().exists():
                    break
            assert paths.pid_file().exists()
            state = json.loads(paths.runtime_state_file().read_text(encoding="utf-8"))
            assert state["pid"] == os.getpid()
            assert len(state["generation"]) == 32
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
        from persome.model import human as human_mod

        # All tasks return immediately; FIRST_COMPLETED then unblocks _run and
        # it proceeds through the normal (non-cancelled) shutdown path.
        async def returns() -> None:
            return

        stub_registry = [
            replace(td, create=lambda c, sm: returns()) for td in _build_task_registry()
        ]
        monkeypatch.setattr(
            "persome.daemon._build_task_registry", lambda _receipt=None: stub_registry
        )
        sync_human = MagicMock(return_value=paths.human_file())
        monkeypatch.setattr(human_mod, "sync_live_human_markdown", sync_human)

        await _run(self._no_task_cfg(), capture_only=True)

        sync_human.assert_called_once_with()
        # Health invariant: a cleanly stopped daemon leaves no pid file behind.
        assert not paths.pid_file().exists()
        assert not paths.runtime_state_file().exists()

    async def test_human_projection_failure_does_not_crash_startup(
        self, ac_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from persome.model import human as human_mod

        async def returns() -> None:
            return

        stub_registry = [
            replace(td, create=lambda c, sm: returns()) for td in _build_task_registry()
        ]
        monkeypatch.setattr(
            "persome.daemon._build_task_registry", lambda _receipt=None: stub_registry
        )
        sync_human = MagicMock(side_effect=RuntimeError("synthetic HUMAN.md failure"))
        monkeypatch.setattr(human_mod, "sync_live_human_markdown", sync_human)

        with patch("persome.daemon.logger") as logger:
            await _run(self._no_task_cfg(), capture_only=True)

        sync_human.assert_called_once_with()
        logger.exception.assert_any_call("HUMAN.md startup projection failed")

    async def test_candidate_runtime_creates_no_human_artifacts_before_update_commit(
        self, ac_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from persome import paths
        from persome.model import human as human_mod

        async def returns() -> None:
            return

        stub_registry = [
            replace(td, create=lambda c, sm: returns()) for td in _build_task_registry()
        ]
        monkeypatch.setattr(
            "persome.daemon._build_task_registry", lambda _receipt=None: stub_registry
        )
        paths.update_state_file().write_text("pending\n", encoding="utf-8")
        sync_human = MagicMock(return_value=paths.human_file())
        monkeypatch.setattr(human_mod, "sync_live_human_markdown", sync_human)

        await _run(self._no_task_cfg(), capture_only=True)

        sync_human.assert_not_called()
        assert not paths.human_file().exists()
        assert list(ac_root.glob(".HUMAN.md.*")) == []

    async def test_human_projection_waits_for_update_commit(
        self, ac_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from persome import paths
        from persome.model import human as human_mod

        paths.update_state_file().write_text("pending\n", encoding="utf-8")
        sync_human = MagicMock(return_value=paths.human_file())
        monkeypatch.setattr(human_mod, "sync_live_human_markdown", sync_human)
        monkeypatch.setattr("persome.daemon._HUMAN_UPDATE_POLL_SECONDS", 0.001)

        task = asyncio.create_task(_sync_human_after_update_commit())
        await asyncio.sleep(0.01)
        sync_human.assert_not_called()

        paths.update_state_file().unlink()
        await asyncio.wait_for(task, timeout=1)
        sync_human.assert_called_once_with()

    async def test_rollback_cancels_deferred_human_projection(
        self, ac_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from persome import paths
        from persome.model import human as human_mod

        paths.update_state_file().write_text("pending\n", encoding="utf-8")
        sync_human = MagicMock(return_value=paths.human_file())
        monkeypatch.setattr(human_mod, "sync_live_human_markdown", sync_human)
        monkeypatch.setattr("persome.daemon._HUMAN_UPDATE_POLL_SECONDS", 0.001)

        task = asyncio.create_task(_sync_human_after_update_commit())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        paths.update_state_file().unlink()
        await asyncio.sleep(0.01)
        sync_human.assert_not_called()

    async def test_force_end_failure_does_not_crash_shutdown(
        self, ac_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def returns() -> None:
            return

        stub_registry = [
            replace(td, create=lambda c, sm: returns()) for td in _build_task_registry()
        ]
        monkeypatch.setattr(
            "persome.daemon._build_task_registry", lambda _receipt=None: stub_registry
        )

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

    async def test_runtime_start_never_requests_screen_recording(
        self, ac_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def returns() -> None:
            return

        stub_registry = [
            replace(td, create=lambda c, sm: returns()) for td in _build_task_registry()
        ]
        monkeypatch.setattr(
            "persome.daemon._build_task_registry", lambda _receipt=None: stub_registry
        )
        monkeypatch.setattr(
            "persome.capture.screen_recording.request_screen_recording",
            lambda: pytest.fail("ordinary Runtime startup must never prompt for TCC access"),
        )
        cfg = self._no_task_cfg()

        await _run(cfg, capture_only=True)

    async def test_ingest_without_http_transport_refuses_to_start(self, ac_root: Path) -> None:
        from persome import paths

        cfg = self._no_task_cfg()
        cfg.capture.source = "ingest"

        with pytest.raises(RuntimeError, match="requires the authenticated HTTP"):
            await _run(cfg, capture_only=True)

        assert not paths.pid_file().exists()


def test_shutdown_timeout_constant_is_sane() -> None:
    # Guard against an accidental 0/negative that would make shutdown not wait.
    assert _SHUTDOWN_TIMEOUT_SECONDS > 0


def test_hard_exit_persists_session_before_process_exit(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from persome import paths

    manager = MagicMock()
    paths.pid_file().write_text("123")
    paths.runtime_state_file().write_text("{}")
    exit_mock = MagicMock()
    monkeypatch.setattr("persome.daemon.os._exit", exit_mock)

    _hard_exit(manager)

    manager.force_end.assert_called_once_with(reason="daemon-shutdown")
    assert not paths.pid_file().exists()
    assert not paths.runtime_state_file().exists()
    exit_mock.assert_called_once_with(0)
