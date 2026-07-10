"""Global event bus for streaming agent activity to live watchers.

Thread-safe: pipeline stages run in ``asyncio.to_thread`` or raw worker threads
and call ``publish`` from there. Local subscribers drain their own
``asyncio.Queue``.

Each subscriber records **its own** running loop at subscription time, and
``publish`` schedules the put on that loop. This is deliberate: the daemon's
background tasks (where ``init`` used to capture *a* loop) and local consumers
are not guaranteed to share one event loop. Posting to a single globally-captured
loop dropped every event when the subscriber lived in a different loop. Capturing
the subscriber's loop removes that coupling entirely.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any

# (queue, loop) pairs for each live subscriber. Modified under _lock; the
# loop is the one the subscriber's async iterator runs on.
_subscribers: list[tuple[asyncio.Queue[dict[str, Any] | None], asyncio.AbstractEventLoop]] = []
_lock = threading.Lock()

_QUEUE_MAXSIZE = 500


def init(_loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Retained for backward compatibility; no longer needed.

    Subscribers now capture their own loop, so the bus doesn't depend on a
    globally-registered loop. Kept as a no-op so existing callers
    (``daemon.py`` startup) don't break.
    """


def publish(stage: str, event_type: str, payload: dict[str, Any]) -> None:
    """Publish an event from any thread to every live subscriber.

    No-op when there are no subscribers. Each event is scheduled on the
    subscriber's own loop, so cross-loop delivery (background thread →
    HTTP-server loop) works. Full queues / dead loops are silently skipped.
    """
    event: dict[str, Any] = {"stage": stage, "type": event_type, **payload}
    with _lock:
        subs = list(_subscribers)
    for q, loop in subs:
        with suppress(asyncio.QueueFull, RuntimeError):
            loop.call_soon_threadsafe(q.put_nowait, event)


def make_on_event(stage: str) -> Callable[[str, dict[str, Any]], None]:
    """Return an ``OnEventFn`` that publishes every LLM event under *stage*."""

    def _on_event(event_type: str, payload: dict[str, Any]) -> None:
        publish(stage, event_type, payload)

    return _on_event


class _Subscription:
    def __init__(self) -> None:
        self._q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._loop: asyncio.AbstractEventLoop | None = None

    async def __aenter__(self) -> _Subscription:
        self._loop = asyncio.get_running_loop()
        with _lock:
            _subscribers.append((self._q, self._loop))
        return self

    async def __aexit__(self, *_: object) -> None:
        with _lock, suppress(ValueError):
            _subscribers.remove((self._q, self._loop))  # type: ignore[arg-type]

    def __aiter__(self) -> _Subscription:
        return self

    async def __anext__(self) -> dict[str, Any]:
        item = await self._q.get()
        if item is None:
            raise StopAsyncIteration
        return item


@asynccontextmanager
async def subscribe() -> AsyncIterator[_Subscription]:
    """Async context manager yielding an async-iterable subscription."""
    sub = _Subscription()
    async with sub:
        yield sub
