"""Serialization for book generation (issue #354).

Book generation runs the same two storage-mutating steps from **two** entry
points that can fire concurrently:

- ``POST /book/generate`` (:func:`persome.api.book_generate_routes.generate_book`),
  the app's manual "generate now" button, and
- the daily Dream run's book sub-steps inside
  :func:`persome.runs.registry._dream_executor`.

Both call :func:`persome.writer.book_page.run_book_pages` and
:func:`persome.writer.book_chapters.run_book_chapters`, which mutate the
same files/tables without internal serialization:

- ``store.book_pages._alloc_stem`` is read-then-write (TOCTOU): two concurrent
  runs can pick the *same* stem, and ``atomic_write_text`` then overwrites
  unconditionally → one run's page is silently lost.
- ``store.book_chapters.replace_generated`` does ``DELETE WHERE edited=0`` then
  re-INSERTs in separate statements: two concurrent runs interleave into
  duplicated / missing chapters.

The dream path migrated off the old ``dream._dream_lock`` onto the
``agent_runs`` ledger + ``run-dispatcher`` (per-kind cap ``dream:1``), so that
lock no longer guards anything and reusing it would NOT serialize against a
real dream. Instead this module owns a **single dedicated lock** that BOTH
entry points pass through.

Both critical sections run on plain synchronous worker threads (FastAPI runs
the sync route handler in its threadpool; the dispatcher runs the executor via
``asyncio.to_thread``), never on the asyncio event loop — so a module-level
:class:`threading.Lock` is the correct primitive and never blocks a loop.

Contention policy ("对外 409、对内串行"):

- The **API** entry point acquires non-blocking and surfaces a 409 to the user
  if generation is already in flight (mirrors ``DreamAlreadyRunningError`` →
  HTTP 409 on ``/dream/run``) — never queue a user click behind a long run.
- The **dream sub-step** is an internal background task; it acquires *blocking*
  so it serializes behind any in-flight generation rather than dropping work.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

__all__ = [
    "BookGenerateInProgressError",
    "book_generate_lock",
    "try_acquire_book_generate",
    "release_book_generate",
    "book_generate_guard",
]

# The single lock guarding all book generation. Both the API route and the
# dream executor's book sub-steps acquire it; locking only one side is as good
# as not locking at all.
book_generate_lock = threading.Lock()


class BookGenerateInProgressError(RuntimeError):
    """Raised when book generation is requested while another run holds the
    lock. The API layer maps this to HTTP 409."""


def try_acquire_book_generate() -> bool:
    """Non-blocking acquire. Returns ``True`` iff the caller now holds the lock
    (and must later call :func:`release_book_generate`)."""
    return book_generate_lock.acquire(blocking=False)


def release_book_generate() -> None:
    """Release a lock taken via :func:`try_acquire_book_generate` or
    :func:`book_generate_guard`. Releasing without holding it is a bug
    (``threading.Lock`` raises ``RuntimeError``)."""
    book_generate_lock.release()


@contextmanager
def book_generate_guard(*, blocking: bool) -> Iterator[None]:
    """Hold :data:`book_generate_lock` for the duration of the ``with`` block.

    ``blocking=True`` waits for the lock (internal/background callers serialize);
    ``blocking=False`` raises :class:`BookGenerateInProgressError` immediately
    when the lock is already held (user-facing callers fail fast → 409).

    The lock is always released on exit, including on exceptions.
    """
    acquired = book_generate_lock.acquire(blocking=blocking)
    if not acquired:
        # Only reachable with blocking=False.
        raise BookGenerateInProgressError("book generation already in progress")
    try:
        yield
    finally:
        book_generate_lock.release()
