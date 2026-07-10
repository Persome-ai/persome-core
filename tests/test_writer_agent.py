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
    cfg.memory_delta.apply_enabled = (
        False  # 测 reduce+classify legacy 路径；apply_enabled=True 下 classifier 退役
    )
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
                entries=['[Feishu] 聊天: 和张三确认了评审结论。"周五版本可以发"'],
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
                        "new_entity": "张三",
                        "kind": "person",
                        "quote": "和张三确认了评审结论",
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
