"""Classifier tests exercising the ``fake_llm`` fixture and JSON fixtures.

These complement ``test_classifier.py`` by demonstrating scripted
multi-turn responses loaded from ``tests/fixtures/llm/classifier/*.json``.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta, timezone
from pathlib import Path

from persome import config as config_mod
from persome import paths
from persome.store import entries as entries_mod
from persome.store import fts
from persome.writer import classifier as classifier_mod
from persome.writer import tools as tools_mod

_TZ = timezone(timedelta(hours=8))


def _seed_event_daily(day: str) -> tuple[str, str]:
    """Create event-YYYY-MM-DD.md with one entry; return (filename, entry_id)."""
    name = f"event-{day}.md"
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name=name,
            description=f"Session log for {day}",
            tags=["event", "session", "daily"],
        )
        entry_id = entries_mod.append_entry(
            conn,
            name=name,
            content=(
                "**Session sess_fixture** (10:00–10:45)\n\n"
                "The user spent 45 minutes in Cursor configuring a new "
                'Python project and said: "I prefer Cursor over VSCode."\n\n'
                "- [10:00-10:45, Cursor] edited project-root files, involving —\n"
            ),
            tags=["session", "sid:sess_fixture"],
        )
    return name, entry_id


def _tool_call(name: str, args: dict, cid: str = "c0"):
    """Build a tool_call object matching ``extract_tool_calls`` expectations."""
    from types import SimpleNamespace

    fn = SimpleNamespace(name=name, arguments=__import__("json").dumps(args, ensure_ascii=False))
    return SimpleNamespace(id=cid, function=fn)


def _response(tool_calls: list | None = None, text: str = ""):
    """Build a response object matching ``extract_text`` expectations."""
    from types import SimpleNamespace

    msg = SimpleNamespace(content=text or None, tool_calls=tool_calls or [])
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice])


def _legacy_cfg(ac_root: Path):
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False
    return cfg


def test_classifier_fixture_append_then_commit(ac_root: Path, fake_llm) -> None:
    """Scripted multi-turn: search → append → commit, using add_script."""
    day = "2026-04-21"
    name, entry_id = _seed_event_daily(day)

    fake_llm.add_script(
        "classifier",
        [
            _response([_tool_call("search_memory", {"query": "Cursor"}, cid="c1")]),
            _response(
                [
                    _tool_call(
                        "append",
                        {
                            "path": "user-preferences.md",
                            "content": "User prefers Cursor over VSCode for Python work.",
                            "tags": ["editor", "preference"],
                        },
                        cid="c2",
                    )
                ]
            ),
            _response([_tool_call("commit", {"summary": "recorded editor preference"}, cid="c3")]),
        ],
    )

    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False
    result = classifier_mod.classify_after_reduce(
        cfg,
        session_id="sess_fixture",
        event_daily_path=name,
        just_written_entry_id=entry_id,
    )

    assert result.committed is True
    assert len(result.written_ids) == 1
    assert result.iterations == 3

    pref = (paths.memory_dir() / "user-preferences.md").read_text()
    assert "Cursor over VSCode" in pref

    # Assert call log captures every LLM invocation.
    assert len(fake_llm.calls) == 3
    assert fake_llm.calls[0]["stage"] == "classifier"


def test_entity_index_injected_when_person_file_exists(ac_root: Path, fake_llm) -> None:
    """Entity index appears in user_msg when a person-*.md file exists."""
    day = "2026-04-23"
    name, entry_id = _seed_event_daily(day)

    # Create a person file so _render_entity_index returns something.
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name="person-alice.md",
            description="Alice is a teammate on the ML team",
            tags=["person", "team"],
        )
        entries_mod.append_entry(
            conn,
            name="person-alice.md",
            content="Alice is a senior ML engineer at the company.",
            tags=["role"],
        )

    captured_msgs: list[list] = []

    def capturing_call_llm(cfg, stage, messages, **kwargs):
        captured_msgs.append(messages)
        raise StopIteration

    import unittest.mock as mock

    from persome.writer import llm as llm_mod

    with (
        mock.patch.object(llm_mod, "call_llm", side_effect=capturing_call_llm),
        contextlib.suppress(StopIteration),
    ):
        classifier_mod.classify_after_reduce(
            _legacy_cfg(ac_root),
            session_id="sess_entity_test",
            event_daily_path=name,
            just_written_entry_id=entry_id,
        )

    assert captured_msgs, "call_llm was never invoked"
    user_content = next(m["content"] for m in captured_msgs[0] if m["role"] == "user")
    assert "# Known entities (person / project)" in user_content
    assert "### person-alice.md" in user_content
    assert "Alice is a senior ML engineer" in user_content


def test_entity_index_absent_when_no_person_project_files(ac_root: Path, fake_llm) -> None:
    """Entity section is omitted when no person-* or project-* files exist."""
    day = "2026-04-24"
    name, entry_id = _seed_event_daily(day)

    captured_msgs: list[list] = []

    def capturing_call_llm(cfg, stage, messages, **kwargs):
        captured_msgs.append(messages)
        raise StopIteration

    import unittest.mock as mock

    from persome.writer import llm as llm_mod

    with (
        mock.patch.object(llm_mod, "call_llm", side_effect=capturing_call_llm),
        contextlib.suppress(StopIteration),
    ):
        classifier_mod.classify_after_reduce(
            _legacy_cfg(ac_root),
            session_id="sess_no_entity",
            event_daily_path=name,
            just_written_entry_id=entry_id,
        )

    assert captured_msgs
    user_content = next(m["content"] for m in captured_msgs[0] if m["role"] == "user")
    assert "# Known entities" not in user_content


def test_search_memory_path_prefix_filters_results(ac_root: Path) -> None:
    """search_memory with path_prefix only returns matching files."""
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name="person-bob.md",
            description="Bob is a colleague",
            tags=["person"],
        )
        entries_mod.append_entry(
            conn, name="person-bob.md", content="Bob works in the design team.", tags=["role"]
        )
        entries_mod.create_file(
            conn,
            name="tool-cursor.md",
            description="Cursor IDE notes",
            tags=["tool"],
        )
        entries_mod.append_entry(
            conn, name="tool-cursor.md", content="Bob uses Cursor for coding.", tags=["tool"]
        )

    with fts.cursor() as conn:
        result = tools_mod.tool_search_memory(conn, query="Bob", top_k=10, path_prefix="person-")

    paths_returned = {r["path"] for r in result["results"]}
    assert "person-bob.md" in paths_returned
    assert "tool-cursor.md" not in paths_returned


def _seed_preference_file(content_alpha: str, content_beta: str) -> tuple[str, str, str]:
    """Pre-populate user-preferences.md with two conflicting entries; return (path, id_alpha, id_beta)."""
    path = "user-preferences.md"
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name=path,
            description="User preferences",
            tags=["user", "preference"],
        )
        id_alpha = entries_mod.append_entry(
            conn,
            name=path,
            content=content_alpha,
            tags=["editor", "preference"],
        )
        id_beta = entries_mod.append_entry(
            conn,
            name=path,
            content=content_beta,
            tags=["editor", "preference"],
        )
    return path, id_alpha, id_beta


def test_classifier_abstracts_two_contradicting_entries(ac_root: Path, fake_llm) -> None:
    """When two existing entries conflict with no temporal advantage, the LLM
    supersedes both and appends a higher-level abstraction tagged
    `abstracted-from:<id1>,<id2>`."""
    day = "2026-04-23"
    name, entry_id = _seed_event_daily(day)
    pref_path, id_alpha, id_beta = _seed_preference_file(
        "User uses VSCode for Python work.",
        "User uses Cursor for Python work.",
    )

    abstracted_tag = f"abstracted-from:{id_alpha},{id_beta}"
    fake_llm.add_script(
        "classifier",
        [
            _response([_tool_call("search_memory", {"query": "editor python"}, cid="c1")]),
            _response(
                [
                    _tool_call(
                        "supersede",
                        {
                            "path": pref_path,
                            "old_entry_id": id_alpha,
                            "new_content": "Superseded — see abstracted entry.",
                            "reason": "abstracted into higher-level rule",
                        },
                        cid="c2",
                    )
                ]
            ),
            _response(
                [
                    _tool_call(
                        "supersede",
                        {
                            "path": pref_path,
                            "old_entry_id": id_beta,
                            "new_content": "Superseded — see abstracted entry.",
                            "reason": "abstracted into higher-level rule",
                        },
                        cid="c3",
                    )
                ]
            ),
            _response(
                [
                    _tool_call(
                        "append",
                        {
                            "path": pref_path,
                            "content": (
                                "User alternates between Cursor and VSCode for Python "
                                "depending on the project."
                            ),
                            "tags": ["editor", "preference", abstracted_tag],
                        },
                        cid="c4",
                    )
                ]
            ),
            _response(
                [
                    _tool_call(
                        "commit",
                        {"summary": "abstracted Cursor/VSCode contradiction"},
                        cid="c5",
                    )
                ]
            ),
        ],
    )

    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False
    result = classifier_mod.classify_after_reduce(
        cfg,
        session_id="sess_fixture",
        event_daily_path=name,
        just_written_entry_id=entry_id,
    )

    assert result.committed is True
    # Three writes: 2 supersedes + 1 append. supersede_entry appends a new
    # row to written_ids each call, so we expect 3 total written ids.
    assert len(result.written_ids) == 3
    assert result.iterations == 5

    pref_text = (paths.memory_dir() / pref_path).read_text()
    assert "alternates between Cursor and VSCode" in pref_text
    assert f"abstracted-from:{id_alpha},{id_beta}" in pref_text
    # Both old entries should now carry a #superseded-by marker on the heading.
    assert pref_text.count("#superseded-by:") >= 2


def test_classifier_supersedes_when_newer_has_clear_advantage(ac_root: Path, fake_llm) -> None:
    """When the newer fact clearly replaces the old (e.g. an explicit
    migration), the LLM picks the supersede path — a single supersede call,
    no abstraction synthesis."""
    day = "2026-04-24"
    name, entry_id = _seed_event_daily(day)
    pref_path = "user-tools.md"
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name=pref_path,
            description="Tools the user adopts",
            tags=["user", "tool"],
        )
        old_id = entries_mod.append_entry(
            conn,
            name=pref_path,
            content="User uses Jira for issue tracking.",
            tags=["tool", "tracker"],
        )

    fake_llm.add_script(
        "classifier",
        [
            _response([_tool_call("search_memory", {"query": "issue tracker"}, cid="c1")]),
            _response(
                [
                    _tool_call(
                        "supersede",
                        {
                            "path": pref_path,
                            "old_entry_id": old_id,
                            "new_content": (
                                "User uses Linear for issue tracking (migrated from Jira ~2026-04)."
                            ),
                            "reason": "explicit migration stated by user",
                            "tags": ["tool", "tracker"],
                        },
                        cid="c2",
                    )
                ]
            ),
            _response([_tool_call("commit", {"summary": "switched to Linear"}, cid="c3")]),
        ],
    )

    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False
    result = classifier_mod.classify_after_reduce(
        cfg,
        session_id="sess_fixture",
        event_daily_path=name,
        just_written_entry_id=entry_id,
    )

    assert result.committed is True
    assert len(result.written_ids) == 1
    assert result.iterations == 3

    tools_text = (paths.memory_dir() / pref_path).read_text()
    assert "Linear for issue tracking" in tools_text
    assert "#superseded-by:" in tools_text
    # No abstracted-from tag should appear when only superseding.
    assert "abstracted-from:" not in tools_text


def test_classifier_fixture_forbidden_event_write(ac_root: Path, fake_llm) -> None:
    """The classifier tries to append to event-*; the guard rejects it."""
    day = "2026-04-22"
    name, entry_id = _seed_event_daily(day)

    fake_llm.add_script(
        "classifier",
        [
            _response(
                [
                    _tool_call(
                        "append",
                        {"path": name, "content": "blocked attempt", "tags": ["x"]},
                        cid="c1",
                    )
                ]
            ),
            _response([_tool_call("commit", {"summary": ""}, cid="c2")]),
        ],
    )

    cfg = config_mod.load(ac_root / "config.toml")
    cfg.memory_delta.apply_enabled = False
    result = classifier_mod.classify_after_reduce(
        cfg,
        session_id="sess_forbid",
        event_daily_path=name,
        just_written_entry_id=entry_id,
    )

    assert result.committed is True
    assert result.written_ids == []

    # Event-daily untouched.
    evt = (paths.memory_dir() / name).read_text()
    assert evt.count("**Session sess_forbid**") == 0
