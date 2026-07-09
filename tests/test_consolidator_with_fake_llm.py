"""Tests for the cross-file offline consolidator (writer/consolidator.py).

These exercise the four key paths described in issue #50:
  * deduplication via scripted supersede/supersede/append calls
  * inspect_source resolves an entry back to timeline_blocks
  * dry_run intercepts writes but still records planned ops
  * empty working region exits early without invoking the LLM
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from persome import config as config_mod
from persome import paths
from persome.session import store as session_store
from persome.store import entries as entries_mod
from persome.store import fts
from persome.timeline import store as timeline_store
from persome.writer import consolidator as consolidator_mod

_TZ = timezone(timedelta(hours=8))


# ─── helpers ────────────────────────────────────────────────────────────


def _tool_call(name: str, args: dict, cid: str = "c0"):
    fn = SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False))
    return SimpleNamespace(id=cid, function=fn)


def _response(tool_calls: list | None = None, text: str = ""):
    msg = SimpleNamespace(content=text or None, tool_calls=tool_calls or [])
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice])


def _seed_entry(path: str, session_id: str, body: str) -> str:
    with fts.cursor() as conn:
        if not (paths.memory_dir() / path).exists():
            entries_mod.create_file(
                conn,
                name=path,
                description=f"seeded for {session_id}",
                tags=["test"],
            )
        return entries_mod.append_entry(
            conn,
            name=path,
            content=body,
            tags=[f"sid:{session_id}", "test"],
        )


def _seed_session(session_id: str, start: datetime, end: datetime) -> None:
    with fts.cursor() as conn:
        session_store.insert(
            conn,
            session_store.SessionRow(
                id=session_id,
                start_time=start,
                end_time=end,
                status="reduced",
            ),
        )


def _seed_timeline_block(start: datetime, end: datetime, entries: list[str]) -> None:
    with fts.cursor() as conn:
        timeline_store.insert(
            conn,
            timeline_store.TimelineBlock(
                start_time=start,
                end_time=end,
                entries=entries,
                apps_used=["TestApp"],
                capture_count=len(entries),
            ),
        )


# ─── tests ──────────────────────────────────────────────────────────────


def test_consolidator_deduplication(ac_root: Path, fake_llm) -> None:
    """Two entries about the same fact → both superseded, abstraction appended."""
    id_a = _seed_entry(
        "tool-cursor.md",
        "sess_dedup",
        "User prefers Cursor over VSCode for everyday work.",
    )
    id_b = _seed_entry(
        "tool-cursor.md",
        "sess_dedup",
        "Cursor is the user's primary editor.",
    )

    fake_llm.add_script(
        "consolidator",
        [
            _response(
                [
                    _tool_call(
                        "supersede",
                        {
                            "path": "tool-cursor.md",
                            "old_entry_id": id_a,
                            "new_content": "See consolidated entry below.",
                            "reason": "merged into canonical entry",
                            "tags": ["editor"],
                        },
                        cid="c1",
                    )
                ]
            ),
            _response(
                [
                    _tool_call(
                        "supersede",
                        {
                            "path": "tool-cursor.md",
                            "old_entry_id": id_b,
                            "new_content": "See consolidated entry below.",
                            "reason": "merged into canonical entry",
                            "tags": ["editor"],
                        },
                        cid="c2",
                    )
                ]
            ),
            _response(
                [
                    _tool_call(
                        "append",
                        {
                            "path": "tool-cursor.md",
                            "content": (
                                "User uses Cursor as the primary editor and prefers it over VSCode."
                            ),
                            "tags": [
                                "editor",
                                "preference",
                                f"consolidated-from:{id_a},{id_b}",
                            ],
                        },
                        cid="c3",
                    )
                ]
            ),
            _response([_tool_call("commit", {"summary": "merged dup"}, cid="c4")]),
        ],
    )

    cfg = config_mod.load(ac_root / "config.toml")
    result = consolidator_mod.consolidate_region(cfg, ["sess_dedup"], dry_run=False)

    assert result.committed is True
    assert result.dry_run is False
    assert id_a in result.superseded_ids
    assert id_b in result.superseded_ids
    # two supersede returns + one append → three written ids
    assert len(result.written_ids) == 3
    assert result.region_size >= 2

    # The two originals are now marked superseded in FTS.
    with fts.cursor() as conn:
        r_a = conn.execute("SELECT superseded FROM entries WHERE id=?", (id_a,)).fetchone()
        r_b = conn.execute("SELECT superseded FROM entries WHERE id=?", (id_b,)).fetchone()
        assert r_a["superseded"] == 1
        assert r_b["superseded"] == 1

    body = (paths.memory_dir() / "tool-cursor.md").read_text()
    assert "primary editor" in body
    assert f"consolidated-from:{id_a},{id_b}" in body


def test_consolidator_inspect_source(ac_root: Path) -> None:
    """inspect_source resolves entry_id → session_id → timeline_blocks."""
    sid = "sess_inspect"
    start = datetime(2026, 5, 10, 10, 0, tzinfo=_TZ)
    end = datetime(2026, 5, 10, 10, 30, tzinfo=_TZ)
    _seed_session(sid, start, end)
    _seed_timeline_block(
        start,
        start + timedelta(minutes=1),
        ["[TestApp] user opened a Python file"],
    )
    _seed_timeline_block(
        start + timedelta(minutes=1),
        start + timedelta(minutes=2),
        ["[TestApp] user typed an import statement"],
    )
    entry_id = _seed_entry("project-acme.md", sid, "User worked on the acme codebase in Python.")

    with fts.cursor() as conn:
        result = consolidator_mod.tool_inspect_source(conn, entry_id=entry_id)

    assert result["found"] is True
    assert result["session_id"] == sid
    assert "TestApp" in result["text"]
    assert "import statement" in result["text"]


def test_consolidator_inspect_source_missing_session(ac_root: Path) -> None:
    """Entry with a sid tag but no matching sessions row → found=False, not error."""
    entry_id = _seed_entry("project-acme.md", "sess_ghost", "orphaned entry")
    with fts.cursor() as conn:
        result = consolidator_mod.tool_inspect_source(conn, entry_id=entry_id)
    assert result["found"] is False
    assert "sess_ghost" in result.get("reason", "")


def test_consolidator_dry_run(ac_root: Path, fake_llm) -> None:
    """dry_run intercepts writes, but planned ops + written_ids are recorded."""
    id_a = _seed_entry("tool-cursor.md", "sess_dry", "User prefers Cursor over VSCode.")

    fake_llm.add_script(
        "consolidator",
        [
            _response(
                [
                    _tool_call(
                        "supersede",
                        {
                            "path": "tool-cursor.md",
                            "old_entry_id": id_a,
                            "new_content": "Refined wording.",
                            "reason": "polished phrasing",
                            "tags": ["editor"],
                        },
                        cid="c1",
                    )
                ]
            ),
            _response(
                [
                    _tool_call(
                        "append",
                        {
                            "path": "tool-cursor.md",
                            "content": "Abstraction across editor preferences.",
                            "tags": ["editor", f"consolidated-from:{id_a}"],
                        },
                        cid="c2",
                    )
                ]
            ),
            _response([_tool_call("commit", {"summary": "dry plan"}, cid="c3")]),
        ],
    )

    cfg = config_mod.load(ac_root / "config.toml")
    result = consolidator_mod.consolidate_region(cfg, ["sess_dry"], dry_run=True)

    assert result.committed is True
    assert result.dry_run is True
    assert result.written_ids  # non-empty (planned ids)
    assert any(op["op"] == "supersede" for op in result.planned_ops)
    assert any(op["op"] == "append" for op in result.planned_ops)

    # No real DB writes happened: the original entry is NOT marked superseded.
    with fts.cursor() as conn:
        row = conn.execute("SELECT superseded FROM entries WHERE id=?", (id_a,)).fetchone()
        assert row["superseded"] == 0

    # And no new entries appeared.
    body = (paths.memory_dir() / "tool-cursor.md").read_text()
    assert "Abstraction across editor preferences" not in body
    assert "Refined wording" not in body


def test_consolidator_empty_region(ac_root: Path, fake_llm) -> None:
    """No sessions have any entries → early exit, LLM never invoked."""
    cfg = config_mod.load(ac_root / "config.toml")
    result = consolidator_mod.consolidate_region(cfg, ["sess_nonexistent"])

    assert result.committed is False
    assert "no entries" in result.skipped_reason
    assert result.region_size == 0
    assert fake_llm.calls == []  # the LLM was never called
