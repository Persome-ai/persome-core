"""Tests for skill_hints: _validate_skill_hint, skill prefix, and round-trip through produce_block_for_window."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from persome import config as config_mod
from persome import paths
from persome.session import store as session_store
from persome.store import entries as entries_mod
from persome.store import fts
from persome.timeline import aggregator

_TZ = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _stem(ts: datetime) -> str:
    return ts.isoformat().replace(":", "-").replace("+", "p")


def _write_capture(ts: datetime, *, app: str = "Lark", title: str = "standup") -> Path:
    payload = {
        "timestamp": ts.isoformat(),
        "schema_version": 2,
        "trigger": {"event_type": "focus"},
        "window_meta": {"app_name": app, "title": title, "bundle_id": "com.lark.appkit"},
        "focused_element": {"role": "AXTextField", "is_editable": True, "value": "status update"},
        "visible_text": "standup channel",
    }
    path = paths.capture_buffer_dir() / f"{_stem(ts)}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _seed_window(start: datetime) -> tuple[datetime, datetime]:
    _write_capture(start + timedelta(seconds=10))
    _write_capture(start + timedelta(seconds=40))
    return start, start + timedelta(minutes=1)


# ---------------------------------------------------------------------------
# _validate_skill_hint — unit tests
# ---------------------------------------------------------------------------


_REGISTERED_SKILLS = {"skill-morning-standup.md"}


def _good_hint(**overrides) -> dict:
    base = {
        "skill": "skill-morning-standup.md",
        "confidence": 0.82,
        "rationale": "entries show user opened Lark standup channel and typed a status update",
    }
    base.update(overrides)
    return base


def test_validate_skill_hint_accepts_canonical() -> None:
    got = aggregator._validate_skill_hint(_good_hint(), skill_paths=_REGISTERED_SKILLS)
    assert got is not None
    assert got["skill"] == "skill-morning-standup.md"
    assert got["confidence"] == 0.82
    assert "rationale" in got


def test_validate_skill_hint_rejects_non_dict() -> None:
    assert aggregator._validate_skill_hint("not-a-dict", skill_paths=_REGISTERED_SKILLS) is None
    assert aggregator._validate_skill_hint(None, skill_paths=_REGISTERED_SKILLS) is None
    assert aggregator._validate_skill_hint(42, skill_paths=_REGISTERED_SKILLS) is None


def test_validate_skill_hint_rejects_unknown_skill() -> None:
    assert (
        aggregator._validate_skill_hint(
            _good_hint(skill="skill-nonexistent.md"), skill_paths=_REGISTERED_SKILLS
        )
        is None
    )


def test_validate_skill_hint_rejects_empty_skill_paths() -> None:
    assert aggregator._validate_skill_hint(_good_hint(), skill_paths=set()) is None


def test_validate_skill_hint_rejects_low_confidence() -> None:
    assert (
        aggregator._validate_skill_hint(_good_hint(confidence=0.64), skill_paths=_REGISTERED_SKILLS)
        is None
    )


def test_validate_skill_hint_accepts_confidence_at_floor() -> None:
    got = aggregator._validate_skill_hint(
        _good_hint(confidence=0.65), skill_paths=_REGISTERED_SKILLS
    )
    assert got is not None
    assert got["confidence"] == 0.65


def test_validate_skill_hint_clamps_high_confidence() -> None:
    got = aggregator._validate_skill_hint(
        _good_hint(confidence=1.5), skill_paths=_REGISTERED_SKILLS
    )
    assert got is not None
    assert got["confidence"] == 1.0


def test_validate_skill_hint_rejects_unparseable_confidence() -> None:
    assert (
        aggregator._validate_skill_hint(
            _good_hint(confidence="high"), skill_paths=_REGISTERED_SKILLS
        )
        is None
    )


def test_validate_skill_hint_rejects_blank_rationale() -> None:
    assert (
        aggregator._validate_skill_hint(_good_hint(rationale=""), skill_paths=_REGISTERED_SKILLS)
        is None
    )
    assert (
        aggregator._validate_skill_hint(_good_hint(rationale="  "), skill_paths=_REGISTERED_SKILLS)
        is None
    )


# ---------------------------------------------------------------------------
# skill- prefix allowed in create_file
# ---------------------------------------------------------------------------


def test_skill_prefix_allowed_in_create_file(ac_root: Path) -> None:
    with fts.cursor() as conn:
        path = entries_mod.create_file(
            conn,
            name="skill-test-routine",
            description="Use when user is running the test routine.",
            tags=["skill", "test"],
        )
    assert path.name == "skill-test-routine.md"
    assert path.exists()


# ---------------------------------------------------------------------------
# produce_block_for_window — skill_hints round-trip
# ---------------------------------------------------------------------------


_SKILL_HINTS_PAYLOAD = json.dumps(
    {
        "entries": [
            "[Lark] standup channel: user typed a status update. Involving: —.",
        ],
        "skill_hints": [
            {
                "skill": "skill-morning-standup.md",
                "confidence": 0.82,
                "rationale": "entries show user opened Lark standup and typed a status update",
            }
        ],
    },
    ensure_ascii=False,
)


def test_produce_block_persists_skill_hints(ac_root: Path, fake_llm) -> None:
    # Register the skill file so list_files() finds it.
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name="skill-morning-standup",
            description="Use when user is doing a weekday morning standup in Lark.",
            tags=["skill"],
        )

    start = datetime(2026, 5, 21, 9, 0, tzinfo=_TZ)
    win_start, win_end = _seed_window(start)
    fake_llm.set_default("timeline", _SKILL_HINTS_PAYLOAD)

    cfg = config_mod.load(ac_root / "config.toml")
    block = aggregator.produce_block_for_window(cfg, start=win_start, end=win_end)

    assert block is not None
    assert len(block.skill_hints) == 1
    hint = block.skill_hints[0]
    assert hint["skill"] == "skill-morning-standup.md"
    assert hint["confidence"] == 0.82

    # Round-trip: confirm the column was persisted to DB.
    with fts.cursor() as conn:
        row = conn.execute(
            "SELECT skill_hints FROM timeline_blocks WHERE id = ?",
            (block.id,),
        ).fetchone()
    assert row is not None
    stored = json.loads(row["skill_hints"])
    assert stored[0]["skill"] == "skill-morning-standup.md"


def test_produce_block_empty_skill_hints_when_no_skills_registered(ac_root: Path, fake_llm) -> None:
    # No skill files registered — skill_hints must always be [].
    start = datetime(2026, 5, 21, 9, 2, tzinfo=_TZ)
    win_start, win_end = _seed_window(start)
    fake_llm.set_default("timeline", _SKILL_HINTS_PAYLOAD)

    cfg = config_mod.load(ac_root / "config.toml")
    block = aggregator.produce_block_for_window(cfg, start=win_start, end=win_end)

    assert block is not None
    assert block.skill_hints == []


def test_skill_echo_is_deduplicated_within_session(ac_root: Path, fake_llm) -> None:
    with fts.cursor() as conn:
        path = entries_mod.create_file(
            conn,
            name="skill-morning-standup",
            description="Use when user is doing a weekday morning standup in Lark.",
            tags=["skill"],
        )
        session_store.insert(
            conn,
            session_store.SessionRow(
                id="sess-one",
                start_time=datetime(2026, 5, 21, 9, 0, tzinfo=_TZ),
            ),
        )

    fake_llm.set_default("timeline", _SKILL_HINTS_PAYLOAD)
    cfg = config_mod.load(ac_root / "config.toml")
    for minute in (0, 1):
        win_start, win_end = _seed_window(datetime(2026, 5, 21, 9, minute, tzinfo=_TZ))
        assert aggregator.produce_block_for_window(cfg, start=win_start, end=win_end) is not None

    assert path.read_text(encoding="utf-8").count("Triggered with confidence 0.82") == 1
    with fts.cursor() as conn:
        rows = conn.execute("SELECT session_id, skill_path FROM skill_observations").fetchall()
    assert [(row["session_id"], row["skill_path"]) for row in rows] == [
        ("sess-one", "skill-morning-standup.md")
    ]


def test_skill_echo_counts_again_in_a_new_session(ac_root: Path, fake_llm) -> None:
    with fts.cursor() as conn:
        path = entries_mod.create_file(
            conn,
            name="skill-morning-standup",
            description="Use when user is doing a weekday morning standup in Lark.",
            tags=["skill"],
        )
        first = datetime(2026, 5, 21, 9, 0, tzinfo=_TZ)
        second = datetime(2026, 5, 21, 10, 0, tzinfo=_TZ)
        session_store.insert(
            conn,
            session_store.SessionRow(id="sess-one", start_time=first),
        )
        session_store.mark_ended(conn, "sess-one", first + timedelta(minutes=5))
        session_store.insert(
            conn,
            session_store.SessionRow(id="sess-two", start_time=second),
        )

    fake_llm.set_default("timeline", _SKILL_HINTS_PAYLOAD)
    cfg = config_mod.load(ac_root / "config.toml")
    for start in (first, second):
        win_start, win_end = _seed_window(start)
        assert aggregator.produce_block_for_window(cfg, start=win_start, end=win_end) is not None

    assert path.read_text(encoding="utf-8").count("Triggered with confidence 0.82") == 2


def test_produce_block_drops_unregistered_skill_hints(ac_root: Path, fake_llm) -> None:
    # Register a *different* skill — LLM returns a hint for an unregistered one.
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name="skill-code-review",
            description="Use when user is doing a code review.",
            tags=["skill"],
        )

    start = datetime(2026, 5, 21, 9, 4, tzinfo=_TZ)
    win_start, win_end = _seed_window(start)
    # LLM returns a hint for skill-morning-standup.md which is NOT registered.
    fake_llm.set_default("timeline", _SKILL_HINTS_PAYLOAD)

    cfg = config_mod.load(ac_root / "config.toml")
    block = aggregator.produce_block_for_window(cfg, start=win_start, end=win_end)

    assert block is not None
    assert block.skill_hints == []
