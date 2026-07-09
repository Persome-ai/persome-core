"""Tests for Dream stage Memory Consolidation (Task 4).

Tests cover:
- _get_last_dream_run / _set_last_dream_run
- _new_classifier_entries
- _assemble_context consolidation section
- run_dream wires consolidation and calls _set_last_dream_run on commit
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from persome import config as config_mod
from persome import paths
from persome.store import fts
from persome.writer import dream as dream_mod

_TZ = timezone(timedelta(hours=8))


# ─── helpers ────────────────────────────────────────────────────────────────


def _insert_entry(
    conn,
    *,
    id: str,
    path: str,
    prefix: str,
    timestamp: str,
    content: str,
    superseded: int = 0,
) -> None:
    fts.upsert_file(
        conn,
        fts.FileRow(
            path=path,
            prefix=prefix,
            description="test",
            tags="",
            status="active",
            entry_count=1,
            created="2026-05-21",
            updated="2026-05-21",
            needs_compact=0,
        ),
    )
    fts.insert_entry(
        conn,
        id=id,
        path=path,
        prefix=prefix,
        timestamp=timestamp,
        tags="",
        content=content,
        superseded=superseded,
    )


def _make_cfg(*, consolidation_enabled: bool = True) -> config_mod.Config:
    return config_mod.Config(
        dream=config_mod.DreamConfig(
            enabled=True,
            lookback_days=7,
            min_consecutive_days=3,
            min_daily_hours=3.0,
            min_sequence_occurrences=2,
            consolidation_enabled=consolidation_enabled,
        ),
        writer=config_mod.WriterConfig(max_tool_iterations=3),
    )


def _tool_call(name: str, args: dict[str, Any], cid: str = "c0") -> Any:
    fn = SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False))
    return SimpleNamespace(id=cid, function=fn)


def _response(tool_calls: list | None = None, text: str = "") -> Any:
    msg = SimpleNamespace(content=text or None, tool_calls=tool_calls or [])
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice])


class _Scripted:
    def __init__(self, responses: list) -> None:
        self.responses = responses
        self.idx = 0

    def __call__(self, *a: Any, **k: Any) -> Any:
        r = self.responses[self.idx]
        self.idx += 1
        return r


# ─── _get_last_dream_run ────────────────────────────────────────────────────


def test_get_last_dream_run_defaults_to_24h_ago_when_missing(ac_root: Path) -> None:
    before = datetime.now().astimezone() - timedelta(hours=24, seconds=5)
    result = dream_mod._get_last_dream_run()
    after = datetime.now().astimezone() - timedelta(hours=24)
    assert before <= result <= after


def test_get_last_dream_run_reads_from_file(ac_root: Path) -> None:
    ts = datetime(2026, 5, 20, 10, 0, 0, tzinfo=_TZ)
    path = paths.root() / dream_mod._LAST_RUN_FILE
    path.write_text(json.dumps({"ts": ts.isoformat()}), encoding="utf-8")

    result = dream_mod._get_last_dream_run()
    assert result == ts


def test_set_last_dream_run_writes_file(ac_root: Path) -> None:
    before = datetime.now().astimezone()
    dream_mod._set_last_dream_run()
    after = datetime.now().astimezone()

    path = paths.root() / dream_mod._LAST_RUN_FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    written = datetime.fromisoformat(data["ts"])
    assert before <= written <= after


# ─── _new_classifier_entries ────────────────────────────────────────────────


def test_new_classifier_entries_empty_when_no_entries(ac_root: Path) -> None:
    since = datetime(2026, 5, 20, 0, 0, tzinfo=_TZ)
    with fts.cursor() as conn:
        result = dream_mod._new_classifier_entries(conn, since)
    assert result == {}


def test_new_classifier_entries_grouped_by_path(ac_root: Path) -> None:
    since = datetime(2026, 5, 20, 0, 0, tzinfo=_TZ)
    ts_new = "2026-05-21T10:00:00+08:00"
    ts_old = "2026-05-19T10:00:00+08:00"

    with fts.cursor() as conn:
        # Two entries in user-profile.md (one after since, one before)
        _insert_entry(
            conn,
            id="e1",
            path="user-profile.md",
            prefix="user",
            timestamp=ts_new,
            content="User prefers Python.",
        )
        _insert_entry(
            conn,
            id="e2",
            path="user-profile.md",
            prefix="user",
            timestamp=ts_old,
            content="Old preference.",
        )
        # One entry in project-acme.md (after since)
        _insert_entry(
            conn,
            id="e3",
            path="project-acme.md",
            prefix="project",
            timestamp=ts_new,
            content="Working on OCR backfill.",
        )

        result = dream_mod._new_classifier_entries(conn, since)

    assert "user-profile.md" in result
    assert "project-acme.md" in result
    assert len(result["user-profile.md"]) == 1
    assert result["user-profile.md"][0]["id"] == "e1"
    assert len(result["project-acme.md"]) == 1
    assert result["project-acme.md"][0]["id"] == "e3"


def test_new_classifier_entries_excludes_event_files(ac_root: Path) -> None:
    since = datetime(2026, 5, 20, 0, 0, tzinfo=_TZ)
    ts_new = "2026-05-21T10:00:00+08:00"

    with fts.cursor() as conn:
        # event- entry should not appear
        _insert_entry(
            conn,
            id="ev1",
            path="event-2026-05-21.md",
            prefix="event",
            timestamp=ts_new,
            content="Session summary.",
        )
        # user- entry should appear
        _insert_entry(
            conn,
            id="u1",
            path="user-profile.md",
            prefix="user",
            timestamp=ts_new,
            content="New fact.",
        )

        result = dream_mod._new_classifier_entries(conn, since)

    assert "event-2026-05-21.md" not in result
    assert "user-profile.md" in result


def test_new_classifier_entries_excludes_superseded(ac_root: Path) -> None:
    since = datetime(2026, 5, 20, 0, 0, tzinfo=_TZ)
    ts_new = "2026-05-21T10:00:00+08:00"

    with fts.cursor() as conn:
        _insert_entry(
            conn,
            id="sup1",
            path="user-profile.md",
            prefix="user",
            timestamp=ts_new,
            content="Superseded fact.",
            superseded=1,
        )
        _insert_entry(
            conn,
            id="active1",
            path="user-profile.md",
            prefix="user",
            timestamp=ts_new,
            content="Active fact.",
            superseded=0,
        )

        result = dream_mod._new_classifier_entries(conn, since)

    ids = [e["id"] for e in result.get("user-profile.md", [])]
    assert "sup1" not in ids
    assert "active1" in ids


# ─── _assemble_context consolidation section ────────────────────────────────


def test_assemble_context_includes_consolidation_section_when_entries_present(
    ac_root: Path,
) -> None:
    new_entries = {
        "user-profile.md": [
            {
                "id": "e1",
                "timestamp": "2026-05-21T14:32:00+08:00",
                "body_preview": "User fixed OCR backfill in 3 files.",
            }
        ],
        "project-acme.md": [
            {
                "id": "e2",
                "timestamp": "2026-05-21T15:01:00+08:00",
                "body_preview": "OCR timing median 3s.",
            },
            {
                "id": "e3",
                "timestamp": "2026-05-21T16:00:00+08:00",
                "body_preview": "Added dream consolidation stage.",
            },
        ],
    }

    with fts.cursor() as conn:
        ctx = dream_mod._assemble_context(
            conn=conn,
            app_stats={},
            app_sequences=[],
            routines={},
            repeated_titles=[],
            repeated_urls=[],
            chat_pairs=[],
            lookback_days=7,
            new_entries=new_entries,
        )

    assert "Memory updates since last dream" in ctx
    assert "2 files" in ctx
    assert "3 new entries" in ctx
    assert "user-profile.md" in ctx
    assert "project-acme.md" in ctx
    assert "OCR backfill" in ctx


def test_assemble_context_omits_section_when_no_new_entries(ac_root: Path) -> None:
    with fts.cursor() as conn:
        ctx = dream_mod._assemble_context(
            conn=conn,
            app_stats={},
            app_sequences=[],
            routines={},
            repeated_titles=[],
            repeated_urls=[],
            chat_pairs=[],
            lookback_days=7,
            new_entries={},
        )

    assert "Memory updates since last dream" not in ctx


def test_assemble_context_omits_section_when_new_entries_is_none(ac_root: Path) -> None:
    with fts.cursor() as conn:
        ctx = dream_mod._assemble_context(
            conn=conn,
            app_stats={},
            app_sequences=[],
            routines={},
            repeated_titles=[],
            repeated_urls=[],
            chat_pairs=[],
            lookback_days=7,
        )

    assert "Memory updates since last dream" not in ctx


# ─── run_dream calls _set_last_dream_run on commit ──────────────────────────


def test_run_dream_calls_set_last_run_on_commit(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_cfg(consolidation_enabled=True)

    script = [
        _response([_tool_call("commit", {"summary": "done"}, cid="c1")]),
    ]
    monkeypatch.setattr("persome.writer.llm.call_llm", _Scripted(script))

    last_run_path = paths.root() / dream_mod._LAST_RUN_FILE
    assert not last_run_path.exists()

    with fts.cursor():
        pass  # ensure DB is initialised
    result = dream_mod.run_dream(cfg)

    assert result.committed
    assert last_run_path.exists()
    data = json.loads(last_run_path.read_text(encoding="utf-8"))
    assert "ts" in data


def test_run_dream_does_not_call_set_last_run_on_no_commit(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_cfg(consolidation_enabled=True)
    cfg.dream.max_tool_iterations = 1

    # Return empty text → no commit
    monkeypatch.setattr(
        "persome.writer.llm.call_llm",
        lambda *a, **k: _response(text="no tool calls"),
    )

    with fts.cursor():
        pass  # ensure DB is initialised
    result = dream_mod.run_dream(cfg)

    assert not result.committed
    last_run_path = paths.root() / dream_mod._LAST_RUN_FILE
    assert not last_run_path.exists()


# ─── run_dream skips consolidation when disabled ────────────────────────────


def test_run_dream_skips_consolidation_when_disabled(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_cfg(consolidation_enabled=False)

    captured_contexts: list[str] = []

    def _spy_run_loop(cfg_inner: Any, conn: Any, *, context: str, **_: Any) -> Any:
        captured_contexts.append(context)
        return dream_mod.DreamResult(skipped_reason="spy")

    monkeypatch.setattr(dream_mod, "_run_dream_loop", _spy_run_loop)

    # Insert a user entry that would show up in consolidation
    ts_new = "2026-05-21T10:00:00+08:00"
    with fts.cursor() as conn:
        _insert_entry(
            conn,
            id="u-skipped",
            path="user-profile.md",
            prefix="user",
            timestamp=ts_new,
            content="Should not appear in context when disabled.",
        )

    dream_mod.run_dream(cfg)

    assert len(captured_contexts) == 1
    assert "Memory updates since last dream" not in captured_contexts[0]
