"""Cross-thread / cross-loop delivery for the events bus (``events.py``).

Regression: events were published from worker threads via a single
globally-captured loop (``events.init``). When the subscriber lived in a
*different* loop — the FastMCP/uvicorn HTTP server loop that actually serves
``/events/stream`` — ``call_soon_threadsafe`` posted to a loop with no
subscribers and every event was silently dropped (HUD / dream / intents-debug
live views received nothing). Subscribers now capture their own loop; publish
must reach them, with no ``init`` needed.
"""

from __future__ import annotations

import asyncio
import threading

from persome import events as ev


async def test_publish_from_worker_thread_reaches_subscriber() -> None:
    got: list[dict] = []

    async def reader() -> None:
        async with ev.subscribe() as sub:
            async for e in sub:
                got.append(e)
                break

    task = asyncio.create_task(reader())
    await asyncio.sleep(0.05)  # let the subscriber register
    # Publish from a worker thread, exactly like a pipeline stage / dream does.
    threading.Thread(
        target=lambda: ev.publish("dream", "tool_call", {"name": "search_memory"})
    ).start()
    await asyncio.wait_for(task, timeout=3)

    assert got == [{"stage": "dream", "type": "tool_call", "name": "search_memory"}]


async def test_publish_with_no_subscribers_is_noop() -> None:
    # No subscribers and no init() — must not raise.
    ev.publish("dream", "tool_call", {"name": "x"})


async def test_multiple_subscribers_each_receive() -> None:
    bufs: list[list[dict]] = [[], []]

    async def reader(i: int) -> None:
        async with ev.subscribe() as sub:
            async for e in sub:
                bufs[i].append(e)
                break

    tasks = [asyncio.create_task(reader(i)) for i in range(2)]
    await asyncio.sleep(0.05)
    ev.publish("classifier", "stage_end", {"written": 3})
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=3)

    for b in bufs:
        assert b == [{"stage": "classifier", "type": "stage_end", "written": 3}]
