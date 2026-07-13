"""Lifecycle guards around the local daemon's SQLite ownership."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from persome import cli


class _FakeLock:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _http_config() -> SimpleNamespace:
    return SimpleNamespace(
        mcp=SimpleNamespace(
            auto_start=True,
            transport="streamable-http",
            host="127.0.0.1",
            port=8742,
        )
    )


def test_init_skips_mutable_integrity_recovery_while_daemon_is_running(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_read_pid", lambda: 4242)
    monkeypatch.setattr(
        cli.integrity,
        "check_and_recover",
        lambda: (_ for _ in ()).throw(AssertionError("active DB was touched")),
    )

    cfg = cli._init()

    assert cfg is not None


def test_start_initialization_is_blocked_by_unresolved_authority(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_read_pid", lambda: None)
    monkeypatch.setattr(cli, "_daemon_lock_is_held", lambda: False)
    monkeypatch.setattr(cli.integrity, "check_and_recover", lambda: [])
    paths = cli.paths
    paths.atomic_write_private_text(
        paths.integrity_config_recovery_pending(),
        '{"version": 1, "phase": "authority_unresolved"}',
    )

    with pytest.raises(cli.typer.Exit) as exc:
        cli._init(starting_runtime=True)

    assert exc.value.exit_code == 2


def test_start_short_circuits_before_initialization_when_already_running(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_read_pid", lambda: 4242)
    monkeypatch.setattr(
        cli,
        "_init",
        lambda: (_ for _ in ()).throw(AssertionError("start initialized an active runtime")),
    )

    result = CliRunner().invoke(cli.app, ["start"])

    assert result.exit_code == 1
    assert "Already running (pid 4242)" in result.output


def test_daemon_lifetime_lock_excludes_a_second_start(ac_root) -> None:
    first = cli._acquire_daemon_lock()
    try:
        with pytest.raises(RuntimeError, match="already starting or running"):
            cli._acquire_daemon_lock()
    finally:
        first.close()

    replacement = cli._acquire_daemon_lock()
    replacement.close()


def test_start_lock_failure_never_initializes_or_forks(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_read_pid", lambda: None)
    monkeypatch.setattr(cli, "_fail_if_runtime_state_is_ambiguous", lambda: None)
    monkeypatch.setattr(
        cli,
        "_acquire_daemon_lock",
        lambda: (_ for _ in ()).throw(RuntimeError("another Runtime is starting")),
    )
    monkeypatch.setattr(
        cli,
        "_init",
        lambda **kwargs: pytest.fail("losing start must not initialize the Runtime"),
    )

    result = CliRunner().invoke(cli.app, ["start"])

    assert result.exit_code == 1
    assert "another Runtime is starting" in result.output


def test_non_starting_client_skips_integrity_during_pid_publication_window(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = cli._acquire_daemon_lock()
    monkeypatch.setattr(cli, "_read_pid", lambda: None)
    monkeypatch.setattr(
        cli.integrity,
        "check_and_recover",
        lambda: pytest.fail("active startup window must not mutate SQLite"),
    )
    try:
        assert cli._init() is not None
    finally:
        lock.close()


def test_non_recovering_init_never_inspects_or_repairs_database(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_fail_if_runtime_state_is_ambiguous", lambda: None)
    monkeypatch.setattr(
        cli,
        "_read_pid",
        lambda: pytest.fail("non-recovering init inspected the daemon PID"),
    )
    monkeypatch.setattr(
        cli.integrity,
        "check_and_recover",
        lambda: pytest.fail("non-recovering init touched SQLite"),
    )

    assert cli._init(recover_integrity=False) is not None


def test_mcp_uses_non_recovering_initialization(monkeypatch: pytest.MonkeyPatch) -> None:
    init_kwargs: list[dict[str, bool]] = []
    started: list[bool] = []
    monkeypatch.setattr(cli, "_init", lambda **kwargs: init_kwargs.append(kwargs))

    from persome.mcp import server as mcp_server

    monkeypatch.setattr(mcp_server, "run_stdio", lambda: started.append(True))
    cli.mcp()

    assert init_kwargs == [{"recover_integrity": False}]
    assert started == [True]


def test_mcp_keeps_initialization_notices_off_protocol_stdout(
    ac_root,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli.paths.config_file().unlink(missing_ok=True)

    from persome.mcp import server as mcp_server

    monkeypatch.setattr(mcp_server, "run_stdio", lambda: None)
    cli.mcp()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Created default config" in captured.err


def test_cli_surfaces_database_recovery_and_model_rebuild_next_step(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_read_pid", lambda: None)
    monkeypatch.setattr(cli, "_daemon_lock_is_held", lambda: False)
    monkeypatch.setattr(
        cli.integrity,
        "check_and_recover",
        lambda: [
            cli.integrity.QuarantinedFile(
                kind="database",
                original_path="index.db",
                quarantine_path="index.db.corrupt.test",
                reason="synthetic corruption",
            )
        ],
    )

    result = CliRunner().invoke(cli.app, ["model", "status"])

    assert result.exit_code == 0, result.output
    assert "recovered a damaged local database" in result.output
    assert "persome model build" in result.output


def test_background_start_reports_success_only_after_readiness(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = _FakeLock()
    cfg = _http_config()
    observed: list[str] = []
    monkeypatch.setattr(cli, "_fail_if_runtime_state_is_ambiguous", lambda: None)
    monkeypatch.setattr(cli, "_read_pid", lambda: None)
    monkeypatch.setattr(cli, "_acquire_daemon_lock", lambda: lock)
    monkeypatch.setattr(cli.env_file_mod, "ensure_local_api_token", lambda _path: "existing")
    monkeypatch.setattr(cli, "_init", lambda **_kwargs: cfg)
    monkeypatch.setattr(cli.os, "fork", lambda: 4321)
    monkeypatch.setattr(
        cli,
        "_wait_for_background_start",
        lambda _cfg: (observed.append("ready") or True, "ready", SimpleNamespace(pid=4242)),
    )
    monkeypatch.setattr(
        cli,
        "_terminate_failed_background_start",
        lambda _process: pytest.fail("a ready Runtime must not be terminated"),
    )

    result = CliRunner().invoke(cli.app, ["start"])

    assert result.exit_code == 0, result.output
    assert observed == ["ready"]
    assert lock.closed is True
    assert "Persome started in background." in result.output


def test_background_start_failure_stops_child_and_reports_port_owner(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = _FakeLock()
    cfg = _http_config()
    process = SimpleNamespace(pid=4242)
    terminated: list[object] = []
    monkeypatch.setattr(cli, "_fail_if_runtime_state_is_ambiguous", lambda: None)
    monkeypatch.setattr(cli, "_read_pid", lambda: None)
    monkeypatch.setattr(cli, "_acquire_daemon_lock", lambda: lock)
    monkeypatch.setattr(cli.env_file_mod, "ensure_local_api_token", lambda _path: "existing")
    monkeypatch.setattr(cli, "_init", lambda **_kwargs: cfg)
    monkeypatch.setattr(cli.os, "fork", lambda: 4321)
    monkeypatch.setattr(
        cli,
        "_wait_for_background_start",
        lambda _cfg: (
            False,
            "port 8742 is in use by a different or incompatible service",
            process,
        ),
    )
    monkeypatch.setattr(
        cli,
        "_terminate_failed_background_start",
        lambda value: terminated.append(value) or True,
    )

    result = CliRunner().invoke(cli.app, ["start"])

    assert result.exit_code == 1, result.output
    assert lock.closed is True
    assert terminated == [process]
    assert "Persome started in background." not in result.output
    assert "did not start correctly" in result.output
    assert "incomplete background Runtime was stopped" in result.output
    assert "lsof -nP -iTCP:8742 -sTCP:LISTEN" in result.output
    assert "persome doctor" in result.output


def test_failed_background_cleanup_escalates_only_the_observed_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = SimpleNamespace(pid=4242)
    signals: list[tuple[object, int]] = []
    cleared: list[object] = []
    waits = iter([False, True])
    monkeypatch.setattr(
        cli.runtime_pid,
        "signal_process",
        lambda target, sig: signals.append((target, sig)) or True,
    )
    monkeypatch.setattr(
        cli.runtime_pid,
        "wait_for_exit",
        lambda target, timeout: next(waits),
    )
    monkeypatch.setattr(
        cli,
        "_clear_failed_background_receipts",
        lambda target: cleared.append(target) or True,
    )

    assert cli._terminate_failed_background_start(process) is True
    assert signals == [(process, cli.signal.SIGTERM), (process, cli.signal.SIGKILL)]
    assert cleared == [process]


def test_sigterm_startup_exit_clears_matching_stale_runtime_receipts(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = "b" * 32
    started_at = 1_752_300_000.0
    process = SimpleNamespace(
        pid=4242,
        generation=generation,
        runtime_started_at=started_at,
    )
    cli.paths.atomic_write_private_text(cli.paths.pid_file(), str(process.pid))
    cli.paths.atomic_write_private_text(
        cli.paths.runtime_state_file(),
        json.dumps(
            {
                "schema_version": 1,
                "pid": process.pid,
                "generation": generation,
                "started_at": started_at,
                "updated_at": started_at,
            }
        ),
    )
    signals: list[tuple[object, int]] = []
    monkeypatch.setattr(
        cli.runtime_pid,
        "signal_process",
        lambda target, sig: signals.append((target, sig)) or True,
    )
    monkeypatch.setattr(cli.runtime_pid, "wait_for_exit", lambda _target, _timeout: True)
    monkeypatch.setattr(cli.runtime_pid, "same_process_is_running", lambda _process: False)

    assert cli._terminate_failed_background_start(process) is True
    assert signals == [(process, cli.signal.SIGTERM)]
    assert not cli.paths.pid_file().exists()
    assert not cli.paths.runtime_state_file().exists()


def test_sigterm_graceful_exit_cleanup_is_idempotent(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = SimpleNamespace(
        pid=4242,
        generation="c" * 32,
        runtime_started_at=1_752_300_000.0,
    )
    monkeypatch.setattr(cli.runtime_pid, "signal_process", lambda _target, _sig: True)
    monkeypatch.setattr(cli.runtime_pid, "wait_for_exit", lambda _target, _timeout: True)
    monkeypatch.setattr(cli.runtime_pid, "same_process_is_running", lambda _process: False)

    assert cli._terminate_failed_background_start(process) is True
    assert not cli.paths.pid_file().exists()
    assert not cli.paths.runtime_state_file().exists()


def test_sigkill_cleanup_removes_only_matching_stale_runtime_receipts(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = "a" * 32
    started_at = 1_752_300_000.0
    process = SimpleNamespace(
        pid=4242,
        generation=generation,
        runtime_started_at=started_at,
    )
    cli.paths.atomic_write_private_text(cli.paths.pid_file(), str(process.pid))
    cli.paths.atomic_write_private_text(
        cli.paths.runtime_state_file(),
        json.dumps(
            {
                "schema_version": 1,
                "pid": process.pid,
                "generation": generation,
                "started_at": started_at,
                "updated_at": started_at,
            }
        ),
    )
    monkeypatch.setattr(cli.runtime_pid, "same_process_is_running", lambda _process: False)

    assert cli._clear_failed_background_receipts(process) is True
    assert not cli.paths.pid_file().exists()
    assert not cli.paths.runtime_state_file().exists()
