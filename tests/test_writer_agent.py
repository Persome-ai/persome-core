"""Writer CLI entry point (``writer.agent.run``) — catches up pending sessions."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from persome import config as config_mod
from persome.session import store as session_store
from persome.store import fts
from persome.store import memory_deltas as deltas_store
from persome.timeline import store as timeline_store
from persome.writer import agent
from persome.writer import llm as llm_mod

_TZ = timezone(timedelta(hours=8))


def _tool_call(name: str, args: dict[str, Any], cid: str = "c0") -> Any:
    fn = SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False))
    return SimpleNamespace(id=cid, function=fn)


def _choice_response(tool_calls: list | None = None, text: str = "") -> Any:
    msg = SimpleNamespace(content=text or None, tool_calls=tool_calls or [])
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")])


def test_writer_run_noop_when_nothing_pending(ac_root: Path) -> None:
    cfg = config_mod.load(ac_root / "config.toml")
    result = agent.run(cfg)
    assert result.reduced == 0
    assert result.classified == 0
    assert result.written_ids == []


def test_writer_run_reduces_pending_and_classifies(ac_root: Path, monkeypatch) -> None:
    """One stranded `ended` session → reducer runs → classifier runs."""
    start = datetime(2026, 4, 21, 9, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=5)
    with fts.cursor() as conn:
        timeline_store.insert(
            conn,
            timeline_store.TimelineBlock(
                start_time=start,
                end_time=end,
                entries=["[Cursor] editing, involving —"],
                apps_used=["Cursor"],
                capture_count=1,
            ),
        )
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess_cli",
                start_time=start,
                end_time=end,
                status="ended",
            ),
        )

    # Reducer: one LLM call that returns sub_tasks. Classifier: one commit call.
    reducer_resp = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=json.dumps(
                        {
                            "summary": "cursor work",
                            "sub_tasks": ["[09:00-09:05, Cursor] edit, involving —"],
                        }
                    ),
                    tool_calls=[],
                ),
                finish_reason="stop",
            )
        ]
    )
    classifier_resp = _choice_response([_tool_call("commit", {"summary": ""}, cid="c1")])
    script = [reducer_resp, classifier_resp]

    def fake_call_llm(cfg, stage, *, messages, tools=None, json_mode=False):
        return script.pop(0)

    monkeypatch.setattr(llm_mod, "call_llm", fake_call_llm)

    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False
    result = agent.run(cfg)

    assert result.reduced == 1
    assert result.classified == 1  # classifier called commit (with no writes)

    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, "sess_cli")
    assert row is not None
    assert row.status == "reduced"


def test_writer_run_respects_reducer_disabled(ac_root: Path, monkeypatch) -> None:
    monkeypatch.setenv("PERSOME_TEST_NO_REDUCER", "1")  # marker (informational)
    cfg = config_mod.load(ac_root / "config.toml")
    # Disable reducer at runtime.
    cfg.reducer.enabled = False
    result = agent.run(cfg)
    assert result.reduced == 0


def test_writer_run_limit_counts_existing_modeled_backlog_first(ac_root: Path, monkeypatch) -> None:
    start = datetime(2026, 4, 21, 9, 0, tzinfo=_TZ)
    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="already-reduced",
                start_time=start,
                end_time=start + timedelta(minutes=5),
                status="reduced",
            ),
        )

    seen: dict[str, Any] = {}

    def fake_reduce_all_pending(cfg, *, limit=None):  # type: ignore[no-untyped-def]
        seen["reduction_limit"] = limit
        return []

    finalized: list[str] = []

    def fake_finalize(cfg, *, session_id, **kwargs):  # type: ignore[no-untyped-def]
        finalized.append(session_id)
        return agent.SessionModelResult(session_id=session_id, skipped_reason="test")

    monkeypatch.setattr(agent.session_reducer, "reduce_all_pending", fake_reduce_all_pending)
    monkeypatch.setattr(agent, "finalize_session", fake_finalize)

    cfg = config_mod.load(ac_root / "config.toml")
    agent.run(cfg, limit=1)

    assert seen["reduction_limit"] == 0
    assert finalized == ["already-reduced"]


def test_retry_pending_modeling_only_selects_unmodeled_reduced_sessions(
    ac_root: Path,
    monkeypatch,
) -> None:
    minute = datetime(2026, 4, 21, 9, 0, tzinfo=_TZ)
    start = minute + timedelta(seconds=5)
    end = minute + timedelta(seconds=30)
    with fts.cursor() as conn:
        for session_id, status in (
            ("pending-model", "reduced"),
            ("stage-error", "reduced"),
            ("still-reducing", "ended"),
            ("already-modeled", "reduced"),
        ):
            session_store.insert(
                conn,
                session_store.SessionRow(
                    id=session_id,
                    start_time=start,
                    end_time=end,
                    status=status,
                ),
            )
        session_store.set_model_retry_reason(conn, "pending-model", "awaiting_closing_block")
        session_store.set_model_retry_reason(conn, "stage-error", "stage_error")
        timeline_store.insert(
            conn,
            timeline_store.TimelineBlock(
                start_time=minute,
                end_time=minute + timedelta(minutes=1),
                entries=["[Editor] closing block now materialized"],
                apps_used=["Editor"],
                capture_count=1,
            ),
        )
        session_store.mark_modeled(conn, "already-modeled", start + timedelta(minutes=2))

    seen: list[str] = []

    def fake_finalize(cfg, *, session_id, **_kwargs):  # type: ignore[no-untyped-def]
        seen.append(session_id)
        return agent.SessionModelResult(session_id=session_id, completed=True)

    monkeypatch.setattr(agent, "finalize_session", fake_finalize)
    cfg = config_mod.load(ac_root / "config.toml")
    results = agent.retry_pending_modeling(cfg)

    assert seen == ["pending-model"]
    assert [result.session_id for result in results] == ["pending-model"]


def test_retry_pending_modeling_never_hammers_stage_errors(
    ac_root: Path,
    monkeypatch,
) -> None:
    start = datetime(2026, 4, 21, 9, 0, tzinfo=_TZ)
    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="persistent-stage-error",
                start_time=start,
                end_time=start + timedelta(minutes=1),
                status="reduced",
            ),
        )
        session_store.set_model_retry_reason(conn, "persistent-stage-error", "stage_error")

    calls: list[str] = []

    def fake_finalize(cfg, *, session_id, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(session_id)
        return agent.SessionModelResult(session_id=session_id)

    monkeypatch.setattr(agent, "finalize_session", fake_finalize)
    cfg = config_mod.load(ac_root / "config.toml")

    assert agent.retry_pending_modeling(cfg) == []
    assert agent.retry_pending_modeling(cfg) == []
    assert calls == []


def test_finalizer_aggregates_classifier_closing_block_wait_for_minute_retry(
    ac_root: Path,
    monkeypatch,
) -> None:
    start = datetime(2026, 7, 10, 8, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=1)
    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess-classifier-closing-wait",
                start_time=start,
                end_time=end,
                status="reduced",
            ),
        )

    observed_clocks: dict[str, datetime] = {}
    transaction_clock = end + timedelta(days=30)

    def fake_classify(*_args, **kwargs):  # type: ignore[no-untyped-def]
        observed_clocks["classifier_logical"] = kwargs["stage_clock"]
        observed_clocks["classifier_processing"] = kwargs["processing_clock"]
        return SimpleNamespace(
            committed=False,
            retryable=True,
            skipped_reason="awaiting_closing_block",
        )

    def fake_pattern(*_args, **kwargs):  # type: ignore[no-untyped-def]
        observed_clocks["pattern_logical"] = kwargs["stage_clock"]
        return SimpleNamespace(
            committed=False,
            retryable=False,
            skipped_reason="pattern detector disabled",
        )

    monkeypatch.setattr(agent.classifier_mod, "classify_after_reduce", fake_classify)
    monkeypatch.setattr(agent.pattern_detector_mod, "detect_after_classify", fake_pattern)
    monkeypatch.setattr(
        agent,
        "_model_delta_range",
        lambda *_args, **_kwargs: (
            SimpleNamespace(skipped_reason="no_blocks"),
            "",
        ),
    )

    result = agent.finalize_session(
        config_mod.load(ac_root / "config.toml"),
        session_id="sess-classifier-closing-wait",
        stage_clock=transaction_clock,
    )

    assert not result.completed
    assert result.errors == ["classifier: awaiting_closing_block"]
    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, "sess-classifier-closing-wait")
    assert row is not None
    assert row.model_retry_reason == "awaiting_closing_block"
    assert observed_clocks == {
        "classifier_logical": end,
        "classifier_processing": transaction_clock,
        "pattern_logical": end,
    }


def test_terminal_finalizer_applies_default_person_model(
    ac_root: Path,
    fake_llm,
) -> None:
    """A reduced session mints model state even when no terminal entry was written."""
    start = datetime(2026, 7, 10, 9, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=1)
    with fts.cursor() as conn:
        timeline_store.insert(
            conn,
            timeline_store.TimelineBlock(
                start_time=start,
                end_time=end,
                entries=[
                    '[Feishu] \u804a\u5929: \u548c\u5f20\u4e09\u786e\u8ba4\u4e86\u8bc4\u5ba1\u7ed3\u8bba\u3002"\u5468\u4e94\u7248\u672c\u53ef\u4ee5\u53d1"'
                ],
                apps_used=["Feishu"],
                capture_count=1,
            ),
        )
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess_model_default",
                start_time=start,
                end_time=end,
                status="reduced",
            ),
        )

    fake_llm.set_default(
        "memory_delta",
        json.dumps(
            {
                "entities": [
                    {
                        "new_entity": "\u5f20\u4e09",
                        "kind": "person",
                        "quote": "\u548c\u5f20\u4e09\u786e\u8ba4\u4e86\u8bc4\u5ba1\u7ed3\u8bba",
                        "confidence": 0.9,
                    }
                ],
                "assertions": [],
                "relations": [],
                "events": [],
            },
            ensure_ascii=False,
        ),
    )

    cfg = config_mod.load(ac_root / "config.toml")
    assert cfg.memory_delta.enabled is True
    assert cfg.memory_delta.apply_enabled is True
    result = agent.finalize_session(cfg, session_id="sess_model_default")

    assert result.completed is True
    assert result.delta.written is True
    assert result.delta.applied is True
    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, "sess_model_default")
        delta = conn.execute(
            "SELECT apply_status FROM memory_deltas WHERE session_id=?",
            ("sess_model_default",),
        ).fetchone()
    assert row is not None and row.modeled_at is not None
    assert delta is not None and delta["apply_status"] == "applied"


def test_terminal_missing_capture_provenance_does_not_advance_modeling(
    ac_root: Path,
    fake_llm,
) -> None:
    minute = datetime(2026, 7, 10, 9, 30, tzinfo=_TZ)
    start = minute - timedelta(minutes=1)
    end = minute + timedelta(seconds=30)
    with fts.cursor() as conn:
        timeline_store.insert(
            conn,
            timeline_store.TimelineBlock(
                start_time=start,
                end_time=minute,
                entries=["[Editor] older complete evidence must not be consumed alone"],
                apps_used=["Editor"],
                capture_count=1,
            ),
        )
        timeline_store.insert(
            conn,
            timeline_store.TimelineBlock(
                start_time=minute,
                end_time=minute + timedelta(minutes=1),
                entries=["[Editor] whole closing minute exists but raw provenance expired"],
                apps_used=["Editor"],
                capture_count=1,
            ),
        )
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess_missing_cutoff_provenance",
                start_time=start,
                end_time=end,
                status="reduced",
            ),
        )

    cfg = config_mod.load(ac_root / "config.toml")
    result = agent.finalize_session(cfg, session_id="sess_missing_cutoff_provenance")

    assert not result.completed
    assert result.delta.skipped_reason == "no_cutoff_safe_blocks"
    assert result.errors == [
        "pattern_detector: no_cutoff_safe_blocks",
        "memory_delta: no_cutoff_safe_blocks",
    ]
    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, "sess_missing_cutoff_provenance")
        delta_count = conn.execute("SELECT COUNT(*) FROM memory_deltas").fetchone()[0]
    assert (
        row is not None
        and row.delta_end is None
        and row.modeled_at is None
        and row.model_retry_reason == "no_cutoff_safe_blocks"
    )
    assert delta_count == 0


def test_active_session_flush_mints_model_before_session_end(
    ac_root: Path,
    fake_llm,
) -> None:
    start = datetime(2026, 7, 10, 10, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=1)
    with fts.cursor() as conn:
        timeline_store.insert(
            conn,
            timeline_store.TimelineBlock(
                start_time=start,
                end_time=end,
                entries=["[Feishu] \u548c\u5f20\u4e09\u786e\u8ba4\u4e86\u8bc4\u5ba1\u7ed3\u8bba"],
                apps_used=["Feishu"],
                capture_count=1,
            ),
        )
        session_store.insert(
            conn,
            session_store.SessionRow(id="sess_live", start_time=start, status="active"),
        )
        session_store.set_flush_end(conn, "sess_live", end)

    fake_llm.set_default(
        "memory_delta",
        json.dumps(
            {
                "entities": [
                    {
                        "new_entity": "\u5f20\u4e09",
                        "kind": "person",
                        "quote": "\u548c\u5f20\u4e09\u786e\u8ba4\u4e86\u8bc4\u5ba1\u7ed3\u8bba",
                        "confidence": 0.9,
                    }
                ],
                "assertions": [],
                "relations": [],
                "events": [],
            },
            ensure_ascii=False,
        ),
    )

    cfg = config_mod.load(ac_root / "config.toml")
    result = agent.model_active_session(cfg, session_id="sess_live")
    again = agent.model_active_session(cfg, session_id="sess_live")

    assert result.completed and result.delta.applied
    assert again.completed and again.delta.skipped_reason == "no_window"
    assert len(fake_llm.calls) == 1
    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, "sess_live")
        point_count = conn.execute(
            "SELECT COUNT(*) FROM evo_nodes WHERE file_name='person-\u5f20\u4e09.md'"
        ).fetchone()[0]
        dirty = session_store.get_system_state(conn, "model_structure_dirty")
    assert row is not None and row.status == "active"
    assert row.delta_end == end and row.modeled_at is None
    assert point_count == 1
    assert dirty == "1"


def test_active_model_resumes_failed_apply_before_new_tail(ac_root: Path, fake_llm) -> None:
    start = datetime(2026, 7, 10, 11, 0, tzinfo=_TZ)
    middle = start + timedelta(minutes=1)
    end = middle + timedelta(minutes=1)
    with fts.cursor() as conn:
        for block_start, text in (
            (start, "[Feishu] \u548c\u5f20\u4e09\u786e\u8ba4\u4e86\u8bc4\u5ba1\u7ed3\u8bba"),
            (middle, "[Feishu] \u548c\u674e\u56db\u786e\u8ba4\u4e86\u53d1\u5e03\u7ed3\u8bba"),
        ):
            timeline_store.insert(
                conn,
                timeline_store.TimelineBlock(
                    start_time=block_start,
                    end_time=block_start + timedelta(minutes=1),
                    entries=[text],
                    apps_used=["Feishu"],
                    capture_count=1,
                ),
            )
        session_store.insert(
            conn,
            session_store.SessionRow(id="sess_resume", start_time=start, status="active"),
        )
        session_store.set_flush_end(conn, "sess_resume", end)
        deltas_store.insert(
            conn,
            session_id="sess_resume",
            payload={"entities": [], "assertions": [], "relations": [], "events": []},
            apply_status="failed",
            window_start=start,
            window_end=middle,
            is_final=False,
        )

    fake_llm.set_default(
        "memory_delta",
        json.dumps(
            {
                "entities": [
                    {
                        "new_entity": "\u674e\u56db",
                        "kind": "person",
                        "quote": "\u548c\u674e\u56db\u786e\u8ba4\u4e86\u53d1\u5e03\u7ed3\u8bba",
                        "confidence": 0.9,
                    }
                ],
                "assertions": [],
                "relations": [],
                "events": [],
            },
            ensure_ascii=False,
        ),
    )

    cfg = config_mod.load(ac_root / "config.toml")
    result = agent.model_active_session(cfg, session_id="sess_resume")

    assert result.completed
    assert len(fake_llm.calls) == 1
    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, "sess_resume")
        windows = conn.execute(
            "SELECT window_start, window_end, apply_status FROM memory_deltas"
            " WHERE session_id=? ORDER BY id",
            ("sess_resume",),
        ).fetchall()
    assert row is not None and row.delta_end == end
    assert [(item["window_start"], item["window_end"]) for item in windows] == [
        (start.isoformat(), middle.isoformat()),
        (middle.isoformat(), end.isoformat()),
    ]
    assert all(item["apply_status"] == "applied" for item in windows)
