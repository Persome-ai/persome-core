"""Lifecycle guards around the local daemon's SQLite ownership."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from persome import cli


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

    assert cfg.chat.model


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
