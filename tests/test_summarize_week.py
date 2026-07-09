"""Tests for the summarize-week executor (Phase 3)."""

from __future__ import annotations

from datetime import UTC, datetime

from persome import paths
from persome.config import load as load_config
from persome.runs import recorder, registry
from persome.store import agent_runs as store
from persome.store import fts

# ── helpers ─────────────────────────────────────────────────────────────────


def _insert_recent_entry(conn, idx: int) -> None:
    """Insert a memory entry with a timestamp in the past 7 days."""
    now = datetime.now(tz=UTC)
    ts = now.strftime(f"%Y-%m-%dT0{idx % 24:02d}:00:00")
    fts.insert_entry(
        conn,
        id=f"test-entry-{idx}",
        path=f"event-2026-06-0{idx}.md",
        prefix="event-",
        timestamp=ts,
        tags="",
        content=f"测试记忆条目 {idx}: 完成了任务 {idx}，进展顺利",
    )


# ── tests ────────────────────────────────────────────────────────────────────


def test_summarize_week_in_registry() -> None:
    assert "summarize-week" in registry.KIND_REGISTRY
    spec = registry.KIND_REGISTRY["summarize-week"]
    assert spec.title == "本周周报"


def test_summarize_week_committed_with_entries(ac_root, monkeypatch) -> None:
    """With entries present, executor commits and writes digest file."""
    monkeypatch.setenv("PERSOME_LLM_MOCK", "1")

    with fts.cursor() as conn:
        for i in range(1, 4):
            _insert_recent_entry(conn, i)

    cfg = load_config()

    events: list[tuple[str, dict]] = []

    def capture_event(etype: str, payload: dict) -> None:
        events.append((etype, payload))

    result = registry.KIND_REGISTRY["summarize-week"].run(cfg, capture_event, {})

    assert result.committed is True
    assert result.skipped_reason == ""
    assert len(result.result_refs) == 1
    ref = result.result_refs[0]
    assert ref["type"] == "memory"

    # Digest file must exist
    digest_path = paths.memory_dir() / ref["path"]
    assert digest_path.exists(), f"digest file not found: {digest_path}"
    text = digest_path.read_text(encoding="utf-8")
    assert "本周周报" in text

    # Progress events must have been emitted
    progress_values = [p["value"] for t, p in events if t == "progress"]
    assert progress_values == [0.1, 0.5, 0.9]


def test_summarize_week_skipped_when_no_entries(ac_root, monkeypatch) -> None:
    """With no memory entries, executor returns committed=False with skipped_reason."""
    monkeypatch.setenv("PERSOME_LLM_MOCK", "1")

    cfg = load_config()

    def noop_event(etype: str, payload: dict) -> None:
        pass

    result = registry.KIND_REGISTRY["summarize-week"].run(cfg, noop_event, {})

    assert result.committed is False
    assert result.skipped_reason == "no entries"
    # No file should be written
    mem_dir = paths.memory_dir()
    digest_files = list(mem_dir.glob("digest-*.md"))
    assert len(digest_files) == 0


def test_summarize_week_window_threshold_matches_storage_format(ac_root, monkeypatch) -> None:
    """The 'past 7 days' lower bound must match how entries are stored (#385).

    Entries are stored as *local* wall clock at *minute* precision with no
    offset (`entries._now_iso_minute` → "%Y-%m-%dT%H:%M"); `fts.recent` compares
    them as plain strings. So the window threshold the executor passes to
    `fts.recent(since=...)` must use that exact format, otherwise:
      - a UTC threshold shifts the window by the local offset (e.g. +08:00
        over-collects ~8h), and
      - the old second-precision (19-char) threshold lexicographically outranks
        a boundary-minute (16-char) entry and silently drops it.

    We spy on the `since` the executor actually passes to assert format + value
    — independent of the runner's timezone and of the (mocked) LLM output.
    """
    monkeypatch.setenv("PERSOME_LLM_MOCK", "1")

    from datetime import datetime, timedelta

    captured: dict[str, str] = {}
    real_recent = fts.recent

    def spy_recent(conn, *, since=None, **kwargs):
        captured["since"] = since
        return real_recent(conn, since=since, **kwargs)

    monkeypatch.setattr(registry.fts, "recent", spy_recent)

    # Seed one entry so the executor commits (and thus calls fts.recent).
    with fts.cursor() as conn:
        fts.insert_entry(
            conn,
            id="win-entry",
            path="event-win.md",
            prefix="event-",
            timestamp=datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M"),
            tags="",
            content="窗口测试条目",
        )

    before = datetime.now().astimezone() - timedelta(days=7)
    cfg = load_config()
    registry.KIND_REGISTRY["summarize-week"].run(cfg, lambda _t, _p: None, {})
    after = datetime.now().astimezone() - timedelta(days=7)

    since = captured["since"]
    # Minute precision (16 chars), matching the stored format — NOT 19-char
    # second precision that would drop a boundary-minute entry.
    assert len(since) == len("2026-06-01T09:30")
    # No offset suffix, parses with the storage format.
    parsed = datetime.strptime(since, "%Y-%m-%dT%H:%M")
    # Tracks *local* wall clock (offset-correct): the threshold sits within the
    # 1-minute window around "local now − 7 days" — a UTC threshold would be off
    # by the local offset (hours), failing this on any non-UTC runner.
    assert before.replace(tzinfo=None) - timedelta(minutes=1) <= parsed
    assert parsed <= after.replace(tzinfo=None) + timedelta(minutes=1)


def test_summarize_week_keeps_boundary_minute_entry(ac_root, monkeypatch) -> None:
    """A boundary-minute entry survives the window filter (precision fix, #385).

    With the old 19-char second-precision threshold, an entry stamped exactly at
    the 7-day boundary minute (16 chars) lexicographically compared `<` the
    threshold and was dropped. The local-minute threshold keeps it; an entry a
    minute older is still excluded.
    """
    monkeypatch.setenv("PERSOME_LLM_MOCK", "1")

    from datetime import datetime, timedelta

    boundary = datetime.now().astimezone() - timedelta(days=7)

    def _fmt(dt) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M")

    rows = {
        "win-on-boundary": _fmt(boundary),
        "win-too-old": _fmt(boundary - timedelta(minutes=1)),
    }
    with fts.cursor() as conn:
        for rid, ts in rows.items():
            fts.insert_entry(
                conn,
                id=rid,
                path=f"event-{rid}.md",
                prefix="event-",
                timestamp=ts,
                tags="",
                content=f"{rid} 内容",
            )

    # Read back exactly what the executor's window query would return.
    with fts.cursor() as conn:
        hits = fts.recent(conn, since=_fmt(boundary), limit=80)
    ids = {h.id for h in hits}

    assert "win-on-boundary" in ids  # boundary minute kept
    assert "win-too-old" not in ids  # a minute older excluded


def test_summarize_week_via_run_recorded(ac_root, monkeypatch) -> None:
    """Full recorder integration: queued run goes through to committed."""
    monkeypatch.setenv("PERSOME_LLM_MOCK", "1")

    # Seed a few memory entries
    with fts.cursor() as conn:
        for i in range(1, 3):
            _insert_recent_entry(conn, i)

    cfg = load_config()

    with fts.cursor() as conn:
        rid = store.enqueue(conn, kind="summarize-week", trigger="user", dispatch_source="user")
        store.mark_running(conn, rid)

    recorder.run_recorded(cfg, rid)

    with fts.cursor() as conn:
        run = store.get_run(conn, rid)
        evs = store.list_events(conn, rid)

    assert run.status == "committed"
    assert run.result_refs  # non-empty
    assert any(e.type == "progress" for e in evs)
    assert any(e.type == "stage_end" for e in evs)

    # Digest file on disk
    import json

    refs = json.loads(run.result_refs) if isinstance(run.result_refs, str) else run.result_refs
    assert len(refs) >= 1
    digest_path = paths.memory_dir() / refs[0]["path"]
    assert digest_path.exists()
