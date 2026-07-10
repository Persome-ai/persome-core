"""Request-scoped trace ID for cross-layer log correlation."""

from __future__ import annotations

import uuid
from contextvars import ContextVar

_trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")


def generate_trace_id() -> str:
    """Return a 12-char hex string suitable for log grep."""
    return uuid.uuid4().hex[:12]


def get_trace_id() -> str:
    return _trace_id_var.get()


def set_trace_id(tid: str) -> None:
    _trace_id_var.set(tid)
