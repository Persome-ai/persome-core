"""The stdio MCP server must exit when its spawning client dies.

Stdio servers normally end on stdin EOF, but a client killed without closing
the pipe (its write end inherited by a still-alive session leader) never
delivers EOF. Orphaned ``persome mcp`` processes then accumulate — 22 idle
servers were once found on a developer machine — and any of them can race
``integrity.check_and_recover`` during manual database maintenance. The
watchdog polls ``os.getppid()`` and, once reparented, mirrors
``cli._watch_parent_death``: SIGTERM for a clean FastMCP shutdown, then a
hard-exit backstop.
"""

from __future__ import annotations

import signal

import pytest

from persome.mcp import server as mcp_server


class _ExitCalled(Exception):
    def __init__(self, code: int) -> None:
        self.code = code


def test_watch_loop_sigterms_then_hard_exits_once_reparented() -> None:
    ppids = iter([42, 42, 1])  # parent alive twice, then reparented to launchd
    sleeps: list[float] = []
    kills: list[tuple[int, int]] = []

    def fake_exit(code: int) -> None:
        raise _ExitCalled(code)

    with pytest.raises(_ExitCalled) as exc:
        mcp_server._watch_parent_loop(
            42,
            poll_seconds=3.0,
            grace_seconds=5.0,
            _getppid=lambda: next(ppids),
            _sleep=sleeps.append,
            _kill=lambda pid, sig: kills.append((pid, sig)),
            _exit=fake_exit,
        )
    assert exc.value.code == 0
    assert [sig for _pid, sig in kills] == [signal.SIGTERM]  # graceful first
    # three polls, then the grace window before the backstop exit
    assert sleeps == [3.0, 3.0, 3.0, 5.0]


def test_watch_loop_keeps_running_while_parent_lives() -> None:
    """A stable ppid never triggers shutdown (loop broken by a sentinel)."""

    class _StopPolling(Exception):
        pass

    polls = 0

    def fake_sleep(_seconds: float) -> None:
        nonlocal polls
        polls += 1
        if polls > 5:
            raise _StopPolling

    with pytest.raises(_StopPolling):
        mcp_server._watch_parent_loop(
            42,
            poll_seconds=1.0,
            grace_seconds=1.0,
            _getppid=lambda: 42,
            _sleep=fake_sleep,
            _kill=lambda pid, sig: pytest.fail("must not signal while parent lives"),
            _exit=lambda code: pytest.fail("must not exit while parent lives"),
        )


def test_watchdog_stays_dormant_when_already_reparented_to_launchd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ppid 1 at startup means there is no client to watch — no thread."""
    started: list[object] = []
    monkeypatch.setattr(mcp_server.threading, "Thread", lambda **kw: started.append(kw))
    mcp_server._start_parent_watchdog(_getppid=lambda: 1)
    assert started == []


def test_watchdog_arms_a_daemon_thread_when_a_client_is_watchable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live branch must start a *daemon* thread on the watch loop.

    A non-daemon watchdog would itself keep an orphaned server alive at
    interpreter shutdown — exactly the failure this feature removes.
    """
    captured: dict[str, object] = {}

    class _FakeThread:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def start(self) -> None:
            captured["started"] = True

    monkeypatch.setattr(mcp_server.threading, "Thread", _FakeThread)
    mcp_server._start_parent_watchdog(_getppid=lambda: 42)
    assert captured["started"] is True
    assert captured["daemon"] is True
    assert captured["target"] is mcp_server._watch_parent_loop
    assert captured["args"] == (42,)


def test_run_stdio_starts_the_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[bool] = []
    monkeypatch.setattr(mcp_server, "_start_parent_watchdog", lambda: started.append(True))

    class _FakeServer:
        def run(self) -> None:  # pragma: no cover - trivial
            pass

    monkeypatch.setattr(mcp_server, "build_server", lambda auth_enabled: _FakeServer())
    mcp_server.run_stdio()
    assert started == [True]
