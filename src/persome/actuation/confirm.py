"""Side-effect confirmation round-trip: daemon ↔ Persome app over SSE.

A gated actuation (send / delete / pay / Return-to-submit) must get the user's OK before it fires.
The MCP tool call runs on a FastMCP worker thread; it publishes a `confirm_request` SSE event (the
app's `ActuationConfirmController` shows a dialog) and BLOCKS on a per-id `threading.Event` until the
app POSTs its decision to `POST /actuation/confirm/{id}` — or a timeout elapses, in which case the
action is DENIED (fail-safe). If NOTHING is listening on the SSE stream (app not running), it denies
immediately rather than stalling the agent for the full timeout.

Pure + offline-testable: no asyncio, no app needed; a test drives `resolve` against an id directly.

Plan: docs/superpowers/plans/2026-06-25-persome-actuation-layer-plan.md §3.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field

from .. import events
from ..logger import get

logger = get("persome.actuation.confirm")

# How long a gated action waits for the user before failing safe (denying).
DEFAULT_TIMEOUT_SECONDS = 60.0


@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    approved: bool = False


_lock = threading.Lock()
_pending: dict[str, _Pending] = {}


def request(
    summary: str,
    *,
    app: str = "",
    verb: str = "",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> bool:
    """Block until the app approves/denies this gated action, or `timeout` elapses (→ deny).

    Returns True only on an explicit approval. A timeout, or no listener at all, → False (fail-safe).
    Safe to call from any (non-event-loop) worker thread — this is where FastMCP runs tool calls.
    """
    if not events.has_subscribers():
        # No app is listening — nobody can approve. Deny now instead of stalling the agent.
        logger.info("confirm denied: no SSE subscriber to ask (%s)", summary)
        return False
    cid = uuid.uuid4().hex
    pending = _Pending()
    with _lock:
        _pending[cid] = pending
    try:
        events.publish(
            "actuation",
            "confirm_request",
            {"id": cid, "summary": summary, "app": app, "verb": verb},
        )
        if not pending.event.wait(timeout):
            logger.info("confirm %s timed out after %.0fs → denied", cid, timeout)
            return False
        return pending.approved
    finally:
        with _lock:
            _pending.pop(cid, None)


def resolve(cid: str, *, approved: bool) -> bool:
    """Resolve a pending confirm (called by the app's POST). Returns True iff `cid` was waiting."""
    with _lock:
        pending = _pending.get(cid)
    if pending is None:
        return False
    pending.approved = approved
    pending.event.set()
    return True


def pending_ids() -> list[str]:
    """The ids of confirms currently awaiting a decision (for diagnostics)."""
    with _lock:
        return list(_pending)
