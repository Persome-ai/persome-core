"Tests for test case extraction."

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from persome.evomem.engine import EvoMemory
from persome.evomem.models import MemoryLayer
from persome.store import fts
from persome.timeline import store as tl_store
from persome.writer import case_extractor

# ── helpers ─────────────────────────────────────────────────────────────────


def _cfg(*, enabled: bool) -> Any:
    return SimpleNamespace(case_extraction_enabled=enabled)


def _resp(payload: dict) -> Any:
    """An OpenAI-shaped response object whose content is JSON."""
    msg = SimpleNamespace(content=json.dumps(payload, ensure_ascii=False), tool_calls=[])
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="stop")],
        usage=SimpleNamespace(total_tokens=0),
    )


def _insert_block(entries: list[str], *, minutes_ago: int = 30) -> None:
    start = datetime.now().astimezone() - timedelta(minutes=minutes_ago)
    block = tl_store.TimelineBlock(
        start_time=start,
        end_time=start + timedelta(minutes=1),
        timezone="+08:00",
        entries=entries,
        apps_used=["Terminal"],
        capture_count=len(entries),
    )
    with fts.cursor() as conn:
        tl_store.insert(conn, block)


def _scripted_llm(cards: list[dict]):
    """Return a llm_call seam that yields ``cards`` FIFO as JSON responses."""
    queue = list(cards)
    captured: list[dict] = []

    def call(cfg: Any, stage: str, messages: list[dict[str, Any]]) -> Any:
        captured.append({"stage": stage, "messages": messages})
        payload = queue.pop(0) if queue else {"problem": "", "solution": ""}
        return _resp(payload)

    call.captured = captured  # type: ignore[attr-defined]
    return call


# ── deterministic pre-filter ────────────────────────────────────────────────


def test_prefilter_pairs_error_with_following_resolution() -> None:
    block = tl_store.TimelineBlock(
        start_time=datetime.now().astimezone(),
        end_time=datetime.now().astimezone() + timedelta(minutes=1),
        entries=[
            "\u8fd0\u884c pytest \u65f6\u62a5\u9519 ModuleNotFoundError: no module named foo",
            "\u68c0\u67e5\u53d1\u73b0\u662f\u4f9d\u8d56\u6ca1\u88c5",
            "pip install foo \u540e\u8dd1\u901a\u4e86\uff0c\u6d4b\u8bd5\u5168\u90e8\u901a\u8fc7",
        ],
    )
    candidates = case_extractor.find_candidates([block])
    assert len(candidates) == 1
    assert "\u62a5\u9519" in candidates[0].error_text or "error" in candidates[0].error_text.lower()
    assert "\u901a\u8fc7" in candidates[0].resolution_text


def test_prefilter_drops_error_without_resolution() -> None:
    block = tl_store.TimelineBlock(
        start_time=datetime.now().astimezone(),
        end_time=datetime.now().astimezone() + timedelta(minutes=1),
        entries=[
            "\u6784\u5efa\u5931\u8d25 build failed with exit code 1",
            "\u53c8\u8bd5\u4e86\u4e00\u6b21\u8fd8\u662f\u4e0d\u884c",
            "\u53bb\u5403\u996d\u4e86",
        ],
    )
    # An error with no resolution signal in the window → no candidate (no half card).
    assert case_extractor.find_candidates([block]) == []


def test_prefilter_ignores_plain_noise() -> None:
    block = tl_store.TimelineBlock(
        start_time=datetime.now().astimezone(),
        end_time=datetime.now().astimezone() + timedelta(minutes=1),
        entries=[
            "\u6253\u5f00\u4e86 Safari \u6d4f\u89c8\u7f51\u9875",
            "\u5728\u5fae\u4fe1\u91cc\u56de\u590d\u4e86\u5f20\u4e09",
            "\u770b\u4e86\u4e00\u4f1a\u513f\u6587\u6863",
        ],
    )
    assert case_extractor.find_candidates([block]) == []


# ── end-to-end extraction → evomem L5 ───────────────────────────────────────


def test_extraction_writes_problem_solution_card_to_l5(ac_root) -> None:
    _insert_block(
        [
            "\u8fd0\u884c\u6d4b\u8bd5\u65f6\u51fa\u73b0 exception: connection refused",
            "\u6392\u67e5\u53d1\u73b0\u670d\u52a1\u6ca1\u8d77\u6765",
            "\u5148 docker compose up \u628a\u4f9d\u8d56\u8d77\u6765\uff0c\u6d4b\u8bd5\u5c31\u901a\u8fc7\u4e86",
        ]
    )
    llm = _scripted_llm(
        [
            {
                "problem": "\u6d4b\u8bd5\u65f6\u8fde\u63a5\u88ab\u62d2\u7edd",
                "solution": "\u5148\u7528 docker compose up \u8d77\u4f9d\u8d56\u518d\u8dd1\u6d4b\u8bd5",
            }
        ]
    )
    mem = EvoMemory()
    result = case_extractor.run_case_extraction(_cfg(enabled=True), llm_call=llm, memory=mem)

    assert result.committed is True
    assert len(result.created_ids) == 1
    assert result.candidates == 1
    assert llm.captured  # the LLM was actually called for the candidate

    # The card is queryable from the store at L5_KNOWLEDGE, routed to topic-cases.md.
    node = mem.store.get(result.created_ids[0])
    assert node is not None
    assert node.layer == MemoryLayer.L5_KNOWLEDGE
    assert node.file_name == case_extractor.CASE_FILE
    assert "docker compose up" in node.content
    assert "\u8fde\u63a5\u88ab\u62d2\u7edd" in node.content

    # Re-querying the active heads also surfaces it.
    heads = mem.store.all_latest()
    assert any(h.node_id == result.created_ids[0] for h in heads)


def test_error_without_resolution_produces_no_card(ac_root) -> None:
    _insert_block(
        [
            "\u90e8\u7f72\u5931\u8d25 deploy failed: permission denied",
            "\u8bd5\u4e86\u597d\u51e0\u6b21\u90fd\u4e0d\u884c",
        ]
    )
    llm = _scripted_llm([{"problem": "x", "solution": "y"}])  # should never be consumed
    mem = EvoMemory()
    result = case_extractor.run_case_extraction(_cfg(enabled=True), llm_call=llm, memory=mem)

    assert result.committed is False
    assert result.created_ids == []
    assert result.candidates == 0
    assert result.skipped_reason == "no candidates"
    assert llm.captured == []  # no LLM call for a non-candidate
    assert mem.store.all_latest() == []


def test_noise_only_timeline_extracts_nothing(ac_root) -> None:
    _insert_block(
        [
            "\u6d4f\u89c8\u4e86\u4e00\u4e9b\u65b0\u95fb",
            "\u56de\u590d\u4e86\u51e0\u5c01\u90ae\u4ef6",
            "\u6574\u7406\u4e86\u684c\u9762\u6587\u4ef6",
        ]
    )
    llm = _scripted_llm([{"problem": "x", "solution": "y"}])
    mem = EvoMemory()
    result = case_extractor.run_case_extraction(_cfg(enabled=True), llm_call=llm, memory=mem)

    assert result.committed is False
    assert result.candidates == 0
    assert llm.captured == []
    assert mem.store.all_latest() == []


def test_half_card_from_llm_is_dropped(ac_root) -> None:
    """A candidate whose LLM card is missing problem or solution → no node."""
    _insert_block(
        [
            "\u7f16\u8bd1\u62a5\u9519 compile error: undefined symbol",
            "\u6539\u4e86\u5934\u6587\u4ef6\u5f15\u7528\u540e\u4fee\u590d\u4e86\uff0cbuild \u6210\u529f",
        ]
    )
    # LLM returns an empty/half card (model judged it not a real problem→solution).
    llm = _scripted_llm([{"problem": "", "solution": ""}])
    mem = EvoMemory()
    result = case_extractor.run_case_extraction(_cfg(enabled=True), llm_call=llm, memory=mem)

    assert result.candidates == 1  # pre-filter found it
    assert llm.captured  # LLM was asked
    assert result.committed is False  # but no valid card was minted
    assert result.created_ids == []
    assert result.skipped_reason == "no cards"
    assert mem.store.all_latest() == []


# ── switch off (default) ────────────────────────────────────────────────────


def test_disabled_is_noop(ac_root) -> None:
    _insert_block(
        [
            "\u6d4b\u8bd5\u62a5\u9519 test failed",
            "\u4fee\u597d\u4e86\uff0c\u5168\u90e8\u901a\u8fc7 resolved",
        ]
    )
    llm = _scripted_llm([{"problem": "p", "solution": "s"}])
    mem = EvoMemory()
    # default off — pass enabled=False explicitly to mirror getattr default.
    result = case_extractor.run_case_extraction(_cfg(enabled=False), llm_call=llm, memory=mem)

    assert result.committed is False
    assert result.skipped_reason == "disabled"
    assert result.created_ids == []
    assert llm.captured == []
    assert mem.store.all_latest() == []


def test_default_getattr_off(ac_root) -> None:
    """A cfg lacking the attribute entirely defaults to OFF (getattr fallback)."""
    _insert_block(["\u62a5\u9519 error happened", "\u4fee\u590d\u4e86 fixed"])
    cfg_without_attr = SimpleNamespace()  # no case_extraction_enabled
    result = case_extractor.run_case_extraction(cfg_without_attr)
    assert result.committed is False
    assert result.skipped_reason == "disabled"
