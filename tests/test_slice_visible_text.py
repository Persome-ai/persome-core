"""Unit tests for _slice_visible_text in timeline/aggregator.py."""

from __future__ import annotations

from persome.timeline.aggregator import _slice_visible_text

LIMIT = 100


def _long(n: int = 200) -> str:
    return "x" * n


# ---------------------------------------------------------------------------
# Short text — no truncation needed
# ---------------------------------------------------------------------------


def test_short_text_returned_unchanged() -> None:
    text = "hello world"
    assert _slice_visible_text(text, "", "com.googlecode.iterm2", LIMIT) == text


def test_exactly_limit_returned_unchanged() -> None:
    text = "a" * LIMIT
    assert _slice_visible_text(text, "", "com.googlecode.iterm2", LIMIT) == text


# ---------------------------------------------------------------------------
# Strategy 1 — focused-value anchor
# ---------------------------------------------------------------------------


def test_anchors_on_focused_value_near_end() -> None:
    # "old noise" fills first 150 chars; then the user typed a long phrase
    typed = "user typed this long meaningful sentence here"
    text = "old noise " * 15 + typed + " more context after"
    result = _slice_visible_text(text, typed, "com.apple.Safari", LIMIT)
    assert typed[:30] in result


def test_anchor_includes_some_pre_context() -> None:
    # 50 % of budget should come before the match
    typed = "y" * 25
    pre = "p" * 120
    text = pre + typed + "a" * 200
    result = _slice_visible_text(text, typed, "com.apple.Safari", LIMIT)
    # some 'p' chars should be present (pre-context)
    assert "p" in result


def test_anchor_adds_ellipsis_prefix_when_start_nonzero() -> None:
    # pre = limit // 2 = 50; match at index 100 → start = 50 > 0 → "…" prefix
    typed = "z" * 25
    text = "a" * 100 + typed + "b" * 200
    result = _slice_visible_text(text, typed, "com.apple.Safari", LIMIT)
    assert result.startswith("…")


def test_anchor_no_ellipsis_prefix_when_start_is_zero() -> None:
    typed = "z" * 25
    text = typed + "b" * 200
    result = _slice_visible_text(text, typed, "com.apple.Safari", LIMIT)
    assert not result.startswith("…")


def test_anchor_search_uses_first_80_chars() -> None:
    typed = "t" * 90  # longer than 80 chars — search uses [:80]
    text = "noise " * 20 + typed + "end" * 10
    # Should still anchor correctly via the 80-char prefix
    result = _slice_visible_text(text, typed, "com.apple.Safari", LIMIT)
    assert "t" * 10 in result


def test_short_focused_value_skips_anchor() -> None:
    # value <= 20 chars: anchor strategy skipped, falls through to strategy 2/3
    text = "a" * 300
    result = _slice_visible_text(text, "short", "com.apple.Safari", LIMIT)
    # Should be head-truncated (strategy 3)
    assert result == "a" * LIMIT + "\n…"


# ---------------------------------------------------------------------------
# Strategy 2 — terminal tail
# ---------------------------------------------------------------------------


def test_terminal_uses_tail_truncation() -> None:
    old = "old session output " * 10
    recent = "recent command output " * 5
    text = old + recent
    result = _slice_visible_text(text, "", "com.googlecode.iterm2", LIMIT)
    last_chars = text[-LIMIT:]
    assert last_chars in result


def test_terminal_tail_has_ellipsis_prefix() -> None:
    text = "x" * 300
    result = _slice_visible_text(text, "", "com.googlecode.iterm2", LIMIT)
    assert result.startswith("…\n")


def test_apple_terminal_also_tail_truncated() -> None:
    text = "a" * 300
    result = _slice_visible_text(text, "", "com.apple.Terminal", LIMIT)
    assert result.startswith("…\n")
    assert text[-LIMIT:] in result


def test_unknown_terminal_app_not_tail_truncated() -> None:
    # An app not in _TERMINAL_BUNDLES should use head truncation
    text = "a" * 300
    result = _slice_visible_text(text, "", "com.someother.app", LIMIT)
    assert result == "a" * LIMIT + "\n…"


def test_cmux_tail_truncated_keeps_injected_terminal_content() -> None:
    # cmux's visible_text is AX chrome (workspace/tab sidebar) at the HEAD,
    # then the injected real terminal surface at the TAIL. Tail-truncation
    # must keep the terminal content and drop the chrome — otherwise the
    # default head-slice would keep the sidebar and cut the actual work.
    chrome = "workspace 1/6 workspace 2/6 update available \u5207\u6362\u4fa7\u8fb9\u680f " * 4
    terminal = "### [cmux terminal] ❯ pytest -k attention ... 12 passed real work here"
    text = chrome + terminal
    result = _slice_visible_text(text, "", "com.cmuxterm.app", LIMIT)
    assert result.startswith("…\n")
    assert text[-LIMIT:] in result
    # the chrome head is what gets dropped
    assert "workspace 1/6" not in result


# ---------------------------------------------------------------------------
# Strategy 3 — default head
# ---------------------------------------------------------------------------


def test_browser_uses_head_truncation() -> None:
    text = "page content " * 30
    result = _slice_visible_text(text, "", "com.google.Chrome", LIMIT)
    assert result == text[:LIMIT] + "\n…"


def test_head_truncation_has_ellipsis_suffix() -> None:
    text = "b" * 300
    result = _slice_visible_text(text, "", "com.example.app", LIMIT)
    assert result.endswith("\n…")


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_visible_text() -> None:
    assert _slice_visible_text("", "anything", "com.googlecode.iterm2", LIMIT) == ""


def test_empty_focused_value_falls_through() -> None:
    text = "x" * 300
    # empty value → skip strategy 1 → terminal tail
    result = _slice_visible_text(text, "", "com.googlecode.iterm2", LIMIT)
    assert result.startswith("…\n")


# ---------------------------------------------------------------------------
# Feishu / chat bundles — tail truncation (newest messages at bottom)
# ---------------------------------------------------------------------------


def test_feishu_uses_tail_truncation() -> None:
    old_msgs = "old message content " * 10
    recent_msgs = "latest reply here " * 6
    text = old_msgs + recent_msgs
    result = _slice_visible_text(text, "", "com.electron.lark", LIMIT)
    assert text[-LIMIT:] in result


def test_feishu_tail_has_ellipsis_prefix() -> None:
    text = "m" * 300
    result = _slice_visible_text(text, "", "com.electron.lark", LIMIT)
    assert result.startswith("…\n")


def test_feishu_short_text_unchanged() -> None:
    text = "short feishu content"
    assert _slice_visible_text(text, "", "com.electron.lark", LIMIT) == text


def test_feishu_focused_value_anchor_takes_priority() -> None:
    # Even for chat bundles, if focused_value is long enough, anchor wins
    typed = "user is typing a long reply in feishu"
    text = "old messages " * 20 + typed + " more"
    result = _slice_visible_text(text, typed, "com.electron.lark", LIMIT)
    assert typed[:20] in result
