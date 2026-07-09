"""Unit tests for the trace module (ContextVar + ID generation)."""

from __future__ import annotations

import re

from persome.trace import generate_trace_id, get_trace_id, set_trace_id


def test_default_empty() -> None:
    assert get_trace_id() == ""


def test_set_and_get() -> None:
    set_trace_id("abc123def456")
    try:
        assert get_trace_id() == "abc123def456"
    finally:
        set_trace_id("")


def test_generate_format() -> None:
    tid = generate_trace_id()
    assert len(tid) == 12
    assert re.fullmatch(r"[0-9a-f]{12}", tid)


def test_generate_unique() -> None:
    ids = {generate_trace_id() for _ in range(100)}
    assert len(ids) == 100
