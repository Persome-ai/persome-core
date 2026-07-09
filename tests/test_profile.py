"""Pure unit tests for the shared cacheable user-profile prefix.

Pins ``build_user_profile``: schema_prior + taste composed (schema first),
either-empty falls through to the other, both-empty → ``""`` (no prefix block),
and the safety-net cap truncates at a newline (never mid-line). The recognizer
threading (cached-prefix placement) is covered by the scaffold snapshots; this
locks the composition logic. Deterministic, offline, no LLM, no DB.
"""

from __future__ import annotations

from persome.intent import profile as prof

_SCHEMA = ["用户偏好晚间集中处理 PRD", "每周五 10 点有站会"]
_TASTE = (
    "# 用户最近在做的事（正先验：这类他真的会动手，识别到相关意图时可适当提高优先级）\n"
    "- 过一版 v2 PRD\n"
    "- 补本周埋点"
)


def test_both_empty_returns_empty() -> None:
    assert prof.build_user_profile(schema_texts=None, taste_text="") == ""
    assert prof.build_user_profile(schema_texts=[], taste_text="") == ""
    assert prof.build_user_profile(schema_texts=None, taste_text="   ") == ""


def test_schema_only_uses_recall_header() -> None:
    out = prof.build_user_profile(schema_texts=_SCHEMA, taste_text="")
    # Byte-identical render to recall.assemble_background's schema_prior block.
    assert out == "# 用户惯性先验\n" + "\n".join(_SCHEMA)
    assert "用户最近在做的事" not in out  # no taste folded in


def test_taste_only_passes_through() -> None:
    # taste carries its own header (built by taste_profile); passed through verbatim.
    assert prof.build_user_profile(schema_texts=None, taste_text=_TASTE) == _TASTE


def test_both_composed_schema_first() -> None:
    out = prof.build_user_profile(schema_texts=_SCHEMA, taste_text=_TASTE)
    assert out.index("# 用户惯性先验") < out.index("# 用户最近在做的事")
    assert "\n\n# 用户最近在做的事" in out  # blank-line separator between the two blocks
    assert out.startswith("# 用户惯性先验\n" + "\n".join(_SCHEMA))


def test_cap_truncates_cleanly_at_newline() -> None:
    big_schema = [f"惯性条目 {i}: " + "y" * 80 for i in range(30)]  # well over _CAP
    out = prof.build_user_profile(schema_texts=big_schema, taste_text="")
    full = "# 用户惯性先验\n" + "\n".join(big_schema)
    assert len(out) <= prof._CAP
    assert full.startswith(out)  # out is a clean prefix, cut at a newline (no mid-line)
