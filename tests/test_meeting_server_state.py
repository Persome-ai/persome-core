"""Regression tests for meeting server `_State` lifecycle locking (issue #439).

The HTTP handlers used to read/use/null `assistant` without a lock, so a
`/transcript` request could deref `None` after a concurrent `/stop`. These tests
pin the atomic semantics of the lock-guarded `_State` methods that replaced that.
"""

import threading
import time

from persome.meeting.server import _State


class _FakeAssistant:
    def __init__(self) -> None:
        self._running = True
        self.fed: list[dict] = []
        self.started = threading.Event()

    def run(self) -> None:
        self.started.set()
        while self._running:
            time.sleep(0.005)

    def feed_transcript(self, **kwargs) -> None:
        self.fed.append(kwargs)


def test_state_start_is_single_flight():
    state = _State()
    created: list[_FakeAssistant] = []

    def factory() -> _FakeAssistant:
        a = _FakeAssistant()
        created.append(a)
        return a

    assert state.start(factory) is True
    assert created[0].started.wait(timeout=2)  # thread is alive
    assert state.start(factory) is False  # already running → no second assistant
    assert len(created) == 1
    assert state.stop() is created[0]


def test_state_stop_returns_clears_and_idempotent():
    state = _State()
    a = _FakeAssistant()
    assert state.start(lambda: a) is True
    assert a.started.wait(timeout=2)

    stopped = state.stop()
    assert stopped is a
    assert a._running is False  # teardown signalled
    assert state.current() is None
    assert state.stop() is None  # second stop is a no-op, not a crash


def test_state_current_snapshot_survives_concurrent_stop():
    # The #439 fix: a snapshot taken by _handle_transcript stays usable even after
    # a concurrent _handle_stop nulls the assistant — no None deref.
    state = _State()
    a = _FakeAssistant()
    state.start(lambda: a)
    assert a.started.wait(timeout=2)

    snapshot = state.current()
    assert state.stop() is a  # concurrent stop clears it
    assert state.current() is None

    assert snapshot is not None
    snapshot.feed_transcript(source="meeting", text="x", is_final=True, sentence_id=0)
    assert a.fed and a.fed[0]["text"] == "x"
