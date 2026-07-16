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


def test_active_model_does_not_advance_watermark_on_apply_result_errors(
    ac_root: Path,
    fake_llm,
    monkeypatch,
) -> None:
    from persome.writer import delta_apply

    start = datetime(2026, 7, 10, 12, 0, tzinfo=_TZ)
    end = start + timedelta(minutes=1)
    with fts.cursor() as conn:
        timeline_store.insert(
            conn,
            timeline_store.TimelineBlock(
                start_time=start,
                end_time=end,
                entries=["[Feishu] confirmed the review result with Alex"],
                apps_used=["Feishu"],
                capture_count=1,
            ),
        )
        session_store.insert(
            conn,
            session_store.SessionRow(id="sess_apply_error", start_time=start, status="active"),
        )
        session_store.set_flush_end(conn, "sess_apply_error", end)
        deltas_store.insert(
            conn,
            session_id="sess_apply_error",
            payload={"entities": [], "assertions": [], "relations": [], "events": []},
            apply_status="failed",
            window_start=start,
            window_end=end,
            is_final=False,
        )
    monkeypatch.setattr(
        delta_apply,
        "apply_delta",
        lambda *args, **kwargs: delta_apply.ApplyResult(errors=["synthetic apply failure"]),
    )

    cfg = config_mod.load(ac_root / "config.toml")
    result = agent.model_active_session(cfg, session_id="sess_apply_error")

    assert not result.completed
    assert result.delta.skipped_reason == "apply_failed"
    assert result.errors == ["memory_delta: apply_failed"]
    assert fake_llm.calls == []
    with fts.cursor() as conn:
        row = session_store.get_by_id(conn, "sess_apply_error")
        delta = deltas_store.latest_for_session(conn, "sess_apply_error")
    assert row is not None and row.delta_end is None
    assert delta is not None and delta["apply_status"] == "failed"
