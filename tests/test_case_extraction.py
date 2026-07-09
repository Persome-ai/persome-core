"""Tests for the case-extraction slow loop (E2: error→resolution「问题→解法卡」).

Covers:
- a timeline with a real error→resolution span → one {problem, solution} card
  lands at L5_KNOWLEDGE and is queryable from the evomem store;
- an error with no following resolution → no half card (deterministic pre-filter
  drops it before any LLM call);
- plain log noise → nothing extracted (pre-filter blocks it);
- switch off (default) → no-op / no output;
- the registered kind is discoverable in the runs registry.
"""

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
            "运行 pytest 时报错 ModuleNotFoundError: no module named foo",
            "检查发现是依赖没装",
            "pip install foo 后跑通了，测试全部通过",
        ],
    )
    candidates = case_extractor.find_candidates([block])
    assert len(candidates) == 1
    assert "报错" in candidates[0].error_text or "error" in candidates[0].error_text.lower()
    assert "通过" in candidates[0].resolution_text


def test_prefilter_drops_error_without_resolution() -> None:
    block = tl_store.TimelineBlock(
        start_time=datetime.now().astimezone(),
        end_time=datetime.now().astimezone() + timedelta(minutes=1),
        entries=[
            "构建失败 build failed with exit code 1",
            "又试了一次还是不行",
            "去吃饭了",
        ],
    )
    # An error with no resolution signal in the window → no candidate (no half card).
    assert case_extractor.find_candidates([block]) == []


def test_prefilter_ignores_plain_noise() -> None:
    block = tl_store.TimelineBlock(
        start_time=datetime.now().astimezone(),
        end_time=datetime.now().astimezone() + timedelta(minutes=1),
        entries=[
            "打开了 Safari 浏览网页",
            "在微信里回复了张三",
            "看了一会儿文档",
        ],
    )
    assert case_extractor.find_candidates([block]) == []


# ── end-to-end extraction → evomem L5 ───────────────────────────────────────


def test_extraction_writes_problem_solution_card_to_l5(ac_root) -> None:
    _insert_block(
        [
            "运行测试时出现 exception: connection refused",
            "排查发现服务没起来",
            "先 docker compose up 把依赖起来，测试就通过了",
        ]
    )
    llm = _scripted_llm(
        [{"problem": "测试时连接被拒绝", "solution": "先用 docker compose up 起依赖再跑测试"}]
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
    assert "连接被拒绝" in node.content

    # Re-querying the active heads also surfaces it.
    heads = mem.store.all_latest()
    assert any(h.node_id == result.created_ids[0] for h in heads)


def test_error_without_resolution_produces_no_card(ac_root) -> None:
    _insert_block(
        [
            "部署失败 deploy failed: permission denied",
            "试了好几次都不行",
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
            "浏览了一些新闻",
            "回复了几封邮件",
            "整理了桌面文件",
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
            "编译报错 compile error: undefined symbol",
            "改了头文件引用后修复了，build 成功",
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
            "测试报错 test failed",
            "修好了，全部通过 resolved",
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
    _insert_block(["报错 error happened", "修复了 fixed"])
    cfg_without_attr = SimpleNamespace()  # no case_extraction_enabled
    result = case_extractor.run_case_extraction(cfg_without_attr)
    assert result.committed is False
    assert result.skipped_reason == "disabled"


# ── registry wiring ─────────────────────────────────────────────────────────


def test_kind_registered() -> None:
    from persome.runs.registry import KIND_REGISTRY

    assert "case-extraction" in KIND_REGISTRY
    spec = KIND_REGISTRY["case-extraction"]
    assert spec.kind == "case-extraction"
    assert spec.title
    assert callable(spec.run)


def test_executor_disabled_run_is_skipped(ac_root) -> None:
    """The registered executor, with the toggle off, returns a skipped outcome."""
    from persome.runs.registry import KIND_REGISTRY

    events: list[tuple[str, dict]] = []

    def on_event(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    spec = KIND_REGISTRY["case-extraction"]
    outcome = spec.run(_cfg(enabled=False), on_event, {})
    assert outcome.committed is False
    assert outcome.skipped_reason == "disabled"
