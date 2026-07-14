"""MCP request knobs are bounded before they reach storage or LLM work."""

from __future__ import annotations

import pytest

from persome.config import Config, MCPConfig
from persome.mcp import captures as captures_mod
from persome.mcp import server as mcp_server
from persome.mcp.limits import (
    bounded_float,
    bounded_int,
    bounded_text,
    bounded_text_list,
)


def test_integer_limits_clamp_both_directions() -> None:
    assert bounded_int(-100, minimum=1, maximum=50) == 1
    assert bounded_int(10_000, minimum=1, maximum=50) == 50


def test_text_limit_rejects_empty_and_oversized_values() -> None:
    with pytest.raises(ValueError, match="query is required"):
        bounded_text("query", "  ", maximum=10)
    with pytest.raises(ValueError, match="query exceeds 10"):
        bounded_text("query", "x" * 11, maximum=10)


def test_text_limit_can_explicitly_allow_empty_values() -> None:
    assert bounded_text("tags", "", maximum=10, allow_empty=True) == ""


def test_list_and_float_limits_reject_resource_abuse() -> None:
    with pytest.raises(ValueError, match="paths exceeds 2 items"):
        bounded_text_list(
            "paths",
            ["a", "b", "c"],
            maximum_items=2,
            maximum_item_chars=10,
        )
    with pytest.raises(ValueError, match="finite"):
        bounded_float(float("nan"), minimum=0, maximum=1)


def test_registered_capture_search_clamps_limit_before_storage(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, int] = {}

    def fake_search(**kwargs):  # type: ignore[no-untyped-def]
        seen["limit"] = kwargs["limit"]
        return []

    monkeypatch.setattr(captures_mod, "search_captures", fake_search)
    server = mcp_server.build_server(Config(), auth_enabled=False)
    search_captures = server._tool_manager._tools["search_captures"].fn

    search_captures(query="bounded", limit=1_000_000)

    assert seen == {"limit": 50}


def test_registered_mcp_tools_reject_oversized_text_before_work(ac_root) -> None:
    server = mcp_server.build_server(Config(), auth_enabled=False)
    tools = server._tool_manager._tools

    with pytest.raises(ValueError, match="query exceeds 20000"):
        tools["search"].fn(query="x" * 20_001)
    with pytest.raises(ValueError, match="correction exceeds 20000"):
        tools["correct_memory"].fn(correction="x" * 20_001)
    with pytest.raises(ValueError, match="tags exceeds 64 items"):
        tools["read_memory"].fn(path="user-profile.md", tags=["x"] * 65)


def test_related_events_gate_and_registered_bounds(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    disabled = mcp_server.build_server(
        Config(mcp=MCPConfig(related_events_enabled=False)),
        auth_enabled=False,
    )
    assert "related_events" not in disabled._tool_manager._tools

    seen: dict[str, int | str] = {}

    def fake_related_events(conn, **kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs)
        return {}

    monkeypatch.setattr(mcp_server, "_related_events", fake_related_events)
    enabled = mcp_server.build_server(Config(), auth_enabled=False)
    tool = enabled._tool_manager._tools["related_events"].fn
    tool(entry_id="entry-1", window_minutes=1_000_000, limit=1_000_000)
    assert seen == {"entry_id": "entry-1", "window_minutes": 1440, "limit": 100}

    with pytest.raises(ValueError, match="entry_id exceeds 256"):
        tool(entry_id="x" * 257)
