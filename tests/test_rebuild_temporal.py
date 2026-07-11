"""Temporal-history regressions for rebuilding the Markdown projection."""

from __future__ import annotations

import sqlite3
import threading

import pytest

from persome.evomem import backfill
from persome.evomem.store import NodeStore
from persome.store import entries as entries_mod
from persome.store import files as files_mod
from persome.store import fts, projector


def _temporal(conn: sqlite3.Connection, *entry_ids: str) -> dict[str, tuple[str, str | None]]:
    placeholders = ", ".join("?" for _ in entry_ids)
    rows = conn.execute(
        f"SELECT entry_id, valid_from, valid_until FROM entry_temporal "
        f"WHERE entry_id IN ({placeholders})",
        entry_ids,
    ).fetchall()
    return {row["entry_id"]: (row["valid_from"], row["valid_until"]) for row in rows}


def _write_raw_file(name: str, body: str) -> None:
    path = files_mod.memory_path(name)
    frontmatter = files_mod.default_frontmatter(description="temporal fixture", tags=[])
    files_mod.write_file(path, frontmatter, body)


def test_rebuild_repairs_and_preserves_multistep_supersede_history(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    times = iter(("2026-07-11T10:00", "2026-07-11T11:00", "2026-07-11T12:00"))
    monkeypatch.setattr(entries_mod, "_now_iso_minute", lambda: next(times))

    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-chain.md", description="chain", tags=[])
        old = entries_mod.append_entry(conn, name="project-chain.md", content="v1", tags=[])
        middle = entries_mod.supersede_entry(
            conn,
            name="project-chain.md",
            old_entry_id=old,
            new_content="v2",
            reason="update",
            tags=[],
        )
        head = entries_mod.supersede_entry(
            conn,
            name="project-chain.md",
            old_entry_id=middle,
            new_content="v3",
            reason="update",
            tags=[],
        )
        expected = {
            old: ("2026-07-11T10:00", "2026-07-11T11:00"),
            middle: ("2026-07-11T11:00", "2026-07-11T12:00"),
            head: ("2026-07-11T12:00", None),
        }
        assert _temporal(conn, old, middle, head) == expected

        # Simulate rows already corrupted by the old rebuild implementation.
        conn.execute(
            "UPDATE entry_temporal SET valid_until=valid_from WHERE entry_id IN (?, ?)",
            (old, middle),
        )
        entries_mod.rebuild_index(conn)
        assert _temporal(conn, old, middle, head) == expected

        # A second production rebuild must be byte-for-byte idempotent.
        entries_mod.rebuild_index(conn)
        assert _temporal(conn, old, middle, head) == expected

    # Backfill consumes entry_temporal directly, so verify the repaired bound
    # reaches the canonical node rather than becoming a durable zero-width row.
    report = backfill.run_backfill()
    assert report.ok
    old_node = NodeStore().get(old)
    middle_node = NodeStore().get(middle)
    assert old_node is not None and old_node.valid_until == "2026-07-11T11:00"
    assert middle_node is not None and middle_node.valid_until == "2026-07-11T12:00"


def test_rebuild_preserves_legacy_orphan_retirement_when_available(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    times = iter(("2026-07-11T10:00", "2026-07-11T13:30"))
    monkeypatch.setattr(entries_mod, "_now_iso_minute", lambda: next(times))

    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-orphan.md", description="orphan", tags=[])
        orphan = entries_mod.append_entry(
            conn, name="project-orphan.md", content="retired", tags=[]
        )
        entries_mod.mark_entry_deleted(conn, name="project-orphan.md", entry_id=orphan)
        assert _temporal(conn, orphan) == {orphan: ("2026-07-11T10:00", "2026-07-11T13:30")}

        # New deletions persist the exact bound in Markdown, so even a fresh
        # projection with no side-table state can recover it losslessly.
        memory_path = files_mod.memory_path("project-orphan.md")
        parsed = files_mod.read_file(memory_path)
        assert "valid-until:2026-07-11T13:30" in parsed.entries[0].tags
        conn.execute("DELETE FROM entry_temporal")
        entries_mod.rebuild_index(conn)
        assert _temporal(conn, orphan) == {orphan: ("2026-07-11T10:00", "2026-07-11T13:30")}

        # Legacy Markdown encodes this as a strike without a successor or an
        # explicit valid-until tag. An in-place rebuild must retain the side
        # table's only copy of the exact retirement time.
        legacy_text = memory_path.read_text().replace(" #valid-until:2026-07-11T13:30", "", 1)
        files_mod.atomic_write_text(memory_path, legacy_text)
        parsed = files_mod.read_file(memory_path)
        assert parsed.entries[0].superseded_by is None
        assert not any(tag.startswith("valid-until:") for tag in parsed.entries[0].tags)
        entries_mod.rebuild_index(conn)
        assert _temporal(conn, orphan) == {orphan: ("2026-07-11T10:00", "2026-07-11T13:30")}

        # With no side-table state, the historical compatibility fallback is
        # necessarily the entry timestamp; there is no exact time in Markdown.
        conn.execute("DELETE FROM entry_temporal")
        entries_mod.rebuild_index(conn)
        assert _temporal(conn, orphan) == {orphan: ("2026-07-11T10:00", "2026-07-11T10:00")}


def test_delete_markdown_bound_closes_pre_db_rebuild_race(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    times = iter(("2026-07-11T10:00", "2026-07-11T13:30"))
    monkeypatch.setattr(entries_mod, "_now_iso_minute", lambda: next(times))

    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-delete-race.md", description="race", tags=[])
        entry_id = entries_mod.append_entry(
            conn, name="project-delete-race.md", content="retire me", tags=[]
        )
        real_retire = entries_mod.derived_retire_rows

        def rebuild_before_db_retire(
            inner_conn: sqlite3.Connection, *, entry_id: str, ts: str
        ) -> None:
            entries_mod.rebuild_index(inner_conn)
            real_retire(inner_conn, entry_id=entry_id, ts=ts)

        monkeypatch.setattr(entries_mod, "derived_retire_rows", rebuild_before_db_retire)
        entries_mod.mark_entry_deleted(conn, name="project-delete-race.md", entry_id=entry_id)

        assert _temporal(conn, entry_id) == {entry_id: ("2026-07-11T10:00", "2026-07-11T13:30")}


def test_delete_anchors_temporal_tag_to_the_real_heading(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_raw_file(
        "project-heading-anchor.md",
        "\n".join(
            (
                "## [2026-07-11T09:00] {id: quoted}",
                "quoted: ## [2026-07-11T10:00] {id: target-id}",
                "",
                "## [2026-07-11T10:00] {id: target-id}",
                "actual target",
            )
        ),
    )
    monkeypatch.setattr(entries_mod, "_now_iso_minute", lambda: "2026-07-11T13:30")

    with fts.cursor() as conn:
        entries_mod.rebuild_index(conn)
        entries_mod.mark_entry_deleted(conn, name="project-heading-anchor.md", entry_id="target-id")
        memory_path = files_mod.memory_path("project-heading-anchor.md")
        parsed = files_mod.read_file(memory_path)
        target = next(entry for entry in parsed.entries if entry.id == "target-id")
        assert "valid-until:2026-07-11T13:30" in target.tags
        assert "quoted: ## [2026-07-11T10:00] {id: target-id} #valid-until" not in (
            memory_path.read_text()
        )

        conn.execute("DELETE FROM entry_temporal")
        entries_mod.rebuild_index(conn)
        assert _temporal(conn, "target-id") == {
            "target-id": ("2026-07-11T10:00", "2026-07-11T13:30")
        }


def test_delete_refined_head_stays_retired_after_rebuild(
    ac_root, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    times = iter(("2026-07-11T10:00", "2026-07-11T11:00", "2026-07-11T13:30"))
    monkeypatch.setattr(entries_mod, "_now_iso_minute", lambda: next(times))

    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-refined.md", description="refined", tags=[])
        old = entries_mod.append_entry(conn, name="project-refined.md", content="rough", tags=[])
        refined = entries_mod.supersede_entry(
            conn,
            name="project-refined.md",
            old_entry_id=old,
            new_content="refined ~~detail~~",
            reason="refine",
            refined_from=old,
            tags=[],
        )
        entries_mod.mark_entry_deleted(conn, name="project-refined.md", entry_id=refined)
        parsed = files_mod.read_file(files_mod.memory_path("project-refined.md"))
        refined_entry = next(entry for entry in parsed.entries if entry.id == refined)
        assert "status:shadow" in refined_entry.tags
        assert refined_entry.body.startswith("refined ~~detail~~")
        assert not entries_mod._body_is_striked(refined_entry.body)
        original_projection = files_mod.memory_path("project-refined.md").read_text()
        assert (
            conn.execute("SELECT superseded FROM entries WHERE id=?", (refined,)).fetchone()[0] == 1
        )
        assert (
            "~~detail~~"
            in conn.execute("SELECT content FROM entries WHERE id=?", (refined,)).fetchone()[0]
        )

        entries_mod.rebuild_index(conn)
        assert (
            conn.execute("SELECT superseded FROM entries WHERE id=?", (refined,)).fetchone()[0] == 1
        )

    report = backfill.run_backfill()
    assert report.ok
    with fts.cursor() as conn:
        projector.project_all(conn, out_dir=tmp_path / "projection")
    assert (tmp_path / "projection" / "project-refined.md").read_text() == original_projection


def test_delete_refined_whole_strike_preserves_legitimate_markdown(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_raw_file(
        "project-refined-strike.md",
        "## [2026-07-11T10:00] {id: refined-strike} "
        "#refined-from:source-id\n~~legitimate struck prose~~\n",
    )
    monkeypatch.setattr(entries_mod, "_now_iso_minute", lambda: "2026-07-11T13:30")

    with fts.cursor() as conn:
        entries_mod.rebuild_index(conn)
        assert (
            conn.execute("SELECT content FROM entries WHERE id='refined-strike'").fetchone()[0]
            == "~~legitimate struck prose~~"
        )
        entries_mod.mark_entry_deleted(
            conn,
            name="project-refined-strike.md",
            entry_id="refined-strike",
        )
        entries_mod.rebuild_index(conn)
        row = conn.execute(
            "SELECT content, superseded FROM entries WHERE id='refined-strike'"
        ).fetchone()
        assert (row["content"], row["superseded"]) == (
            "~~legitimate struck prose~~",
            1,
        )


def test_delete_body_starting_with_inline_strike_stays_retired(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    times = iter(("2026-07-11T10:00", "2026-07-11T13:30"))
    monkeypatch.setattr(entries_mod, "_now_iso_minute", lambda: next(times))

    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-inline.md", description="inline", tags=[])
        entry_id = entries_mod.append_entry(
            conn,
            name="project-inline.md",
            content="~~draft~~ but still live",
            tags=[],
        )
        entries_mod.mark_entry_deleted(conn, name="project-inline.md", entry_id=entry_id)
        entries_mod.rebuild_index(conn)
        assert (
            conn.execute("SELECT superseded FROM entries WHERE id=?", (entry_id,)).fetchone()[0]
            == 1
        )
        assert (
            conn.execute("SELECT content FROM entries WHERE id=?", (entry_id,)).fetchone()[0]
            == "~~draft~~ but still live"
        )


def test_delete_already_superseded_entry_is_projection_idempotent(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    times = iter(("2026-07-11T10:00", "2026-07-11T11:00"))
    monkeypatch.setattr(entries_mod, "_now_iso_minute", lambda: next(times))

    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-repeat.md", description="repeat", tags=[])
        old = entries_mod.append_entry(conn, name="project-repeat.md", content="old", tags=[])
        head = entries_mod.supersede_entry(
            conn,
            name="project-repeat.md",
            old_entry_id=old,
            new_content="head",
            reason="update",
            tags=[],
        )
        memory_path = files_mod.memory_path("project-repeat.md")
        before = memory_path.read_text()
        entries_mod.mark_entry_deleted(conn, name="project-repeat.md", entry_id=old)
        assert memory_path.read_text() == before
        parsed = files_mod.read_file(memory_path)
        old_entry = next(entry for entry in parsed.entries if entry.id == old)
        assert not any(tag.startswith("valid-until:") for tag in old_entry.tags)
        entries_mod.rebuild_index(conn)
        assert _temporal(conn, old, head) == {
            old: ("2026-07-11T10:00", "2026-07-11T11:00"),
            head: ("2026-07-11T11:00", None),
        }


def test_rebuild_uses_global_successors_and_explicit_temporal_overrides(ac_root) -> None:
    _write_raw_file(
        "project-old.md",
        "\n".join(
            (
                "## [2026-07-11T10:00] {id: old-cross} #superseded-by:new-head",
                "~~old cross-file value~~",
                "",
                "## [2026-07-11T10:05] {id: old-explicit} "
                "#superseded-by:new-head #valid-from:2026-07-11T09:00 "
                "#valid-until:2026-07-11T10:30",
                "~~old explicitly bounded value~~",
            )
        ),
    )
    _write_raw_file(
        "project-new.md",
        "## [2026-07-11T11:00] {id: new-head}\ncurrent value\n",
    )

    with fts.cursor() as conn:
        entries_mod.rebuild_index(conn)
        assert _temporal(conn, "old-cross", "old-explicit", "new-head") == {
            "old-cross": ("2026-07-11T10:00", "2026-07-11T11:00"),
            "old-explicit": ("2026-07-11T09:00", "2026-07-11T10:30"),
            "new-head": ("2026-07-11T11:00", None),
        }


def test_dangling_successor_preserves_last_known_temporal_state(ac_root) -> None:
    _write_raw_file(
        "project-dangling.md",
        "## [2026-07-11T10:00] {id: old-dangling} #superseded-by:missing-head\n~~old value~~\n",
    )

    with fts.cursor() as conn:
        entries_mod.rebuild_index(conn)
        conn.execute(
            "UPDATE entry_temporal SET valid_until=? WHERE entry_id=?",
            ("2026-07-11T15:00", "old-dangling"),
        )
        entries_mod.rebuild_index(conn)
        assert _temporal(conn, "old-dangling") == {
            "old-dangling": ("2026-07-11T10:00", "2026-07-11T15:00")
        }


def test_duplicate_id_preflight_leaves_existing_projection_untouched(ac_root) -> None:
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-first.md", description="first", tags=[])
        entry_id = entries_mod.append_entry(
            conn, name="project-first.md", content="original", tags=[]
        )
        before_entries = [tuple(row) for row in conn.execute("SELECT * FROM entries")]
        before_files = [tuple(row) for row in conn.execute("SELECT * FROM files")]
        before_temporal = [tuple(row) for row in conn.execute("SELECT * FROM entry_temporal")]

        _write_raw_file(
            "project-second.md",
            f"## [2026-07-11T12:00] {{id: {entry_id}}}\nduplicate\n",
        )

        with pytest.raises(ValueError, match="duplicate entry id"):
            entries_mod.rebuild_index(conn)

        assert [tuple(row) for row in conn.execute("SELECT * FROM entries")] == before_entries
        assert [tuple(row) for row in conn.execute("SELECT * FROM files")] == before_files
        assert [
            tuple(row) for row in conn.execute("SELECT * FROM entry_temporal")
        ] == before_temporal


def test_evomem_authority_rebuild_preserves_direct_markdown_event_history(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    times = iter(("2026-07-11T10:00", "2026-07-11T11:00"))
    monkeypatch.setattr(entries_mod, "_now_iso_minute", lambda: next(times))

    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="event-2026-07-11.md", description="events", tags=[])
        old = entries_mod.append_entry(conn, name="event-2026-07-11.md", content="v1", tags=[])
        head = entries_mod.supersede_entry(
            conn,
            name="event-2026-07-11.md",
            old_entry_id=old,
            new_content="v2",
            reason="correction",
            tags=[],
        )
        expected = {
            old: ("2026-07-11T10:00", "2026-07-11T11:00"),
            head: ("2026-07-11T11:00", None),
        }
        assert _temporal(conn, old, head) == expected

    NodeStore()  # ensure the canonical table exists even though event files are skipped
    (ac_root / "config.toml").write_text('[evomem]\nwrite_authority = "evomem"\n')
    with fts.cursor() as conn:
        entries_mod.rebuild_index(conn)
        assert _temporal(conn, old, head) == expected
        entries_mod.rebuild_index(conn)
        assert _temporal(conn, old, head) == expected


def test_failed_ingest_rolls_back_the_entire_projection(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-rollback.md", description="r", tags=[])
        entries_mod.append_entry(conn, name="project-rollback.md", content="first", tags=[])
        entries_mod.append_entry(conn, name="project-rollback.md", content="second", tags=[])
        queries = {
            "entries": "SELECT * FROM entries ORDER BY id",
            "entry_temporal": "SELECT * FROM entry_temporal ORDER BY entry_id",
            "entry_metadata": "SELECT * FROM entry_metadata ORDER BY entry_id",
            "files": "SELECT * FROM files ORDER BY path",
        }
        before = {
            table: [tuple(row) for row in conn.execute(query)] for table, query in queries.items()
        }

        real_insert = fts.insert_entry
        calls = 0

        def fail_second_insert(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected ingest failure")
            return real_insert(*args, **kwargs)

        monkeypatch.setattr(fts, "insert_entry", fail_second_insert)
        with pytest.raises(RuntimeError, match="injected ingest failure"):
            entries_mod.rebuild_index(conn)

        after = {
            table: [tuple(row) for row in conn.execute(query)] for table, query in queries.items()
        }
        assert after == before


def test_source_change_during_rebuild_retries_without_stale_projection(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = files_mod.memory_path("project-race.md")
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name=path.name, description="race", tags=[])
        original_id = entries_mod.append_entry(conn, name=path.name, content="original", tags=[])

        real_read = files_mod.read_file
        injected = False

        def read_then_inject(source_path):
            nonlocal injected
            parsed = real_read(source_path)
            if not injected and source_path == path:
                injected = True
                updated = source_path.read_text().rstrip()
                updated += "\n\n## [2026-07-11T12:00] {id: concurrent-new}\nconcurrent content\n"
                files_mod.atomic_write_text(source_path, updated)
            return parsed

        monkeypatch.setattr(files_mod, "read_file", read_then_inject)
        files_count, entries_count = entries_mod.rebuild_index(conn)

        assert injected
        assert (files_count, entries_count) == (1, 2)
        assert (
            conn.execute("SELECT content FROM entries WHERE id=?", (original_id,)).fetchone()[0]
            == "original"
        )
        concurrent = conn.execute(
            "SELECT content FROM entries WHERE id='concurrent-new'"
        ).fetchone()
        assert concurrent is not None and concurrent[0] == "concurrent content"
        assert _temporal(conn, "concurrent-new") == {"concurrent-new": ("2026-07-11T12:00", None)}


def test_writers_paused_after_markdown_do_not_duplicate_rebuilt_entries(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = {"value": "2026-07-11T10:00"}
    monkeypatch.setattr(entries_mod, "_now_iso_minute", lambda: now["value"])
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-writer-race.md", description="race", tags=[])

    def run_paused_write(derived_name, write):
        real_derived = getattr(entries_mod, derived_name)
        markdown_written = threading.Event()
        resume_db_write = threading.Event()
        results: list[str] = []
        errors: list[BaseException] = []

        def paused_derived(*args, **kwargs):
            markdown_written.set()
            if not resume_db_write.wait(timeout=10):
                raise TimeoutError("timed out waiting to resume derived DB write")
            return real_derived(*args, **kwargs)

        def writer() -> None:
            try:
                with fts.cursor() as writer_conn:
                    results.append(write(writer_conn))
            except BaseException as exc:  # surfaced on the main test thread below
                errors.append(exc)

        monkeypatch.setattr(entries_mod, derived_name, paused_derived)
        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        assert markdown_written.wait(timeout=10)
        try:
            with fts.cursor() as rebuild_conn:
                entries_mod.rebuild_index(rebuild_conn)
        finally:
            resume_db_write.set()
        thread.join(timeout=10)
        monkeypatch.setattr(entries_mod, derived_name, real_derived)
        assert not thread.is_alive()
        assert errors == []
        assert len(results) == 1
        return results[0]

    appended = run_paused_write(
        "derived_append_rows",
        lambda conn: entries_mod.append_entry(
            conn,
            name="project-writer-race.md",
            content="first",
            tags=[],
        ),
    )
    with fts.cursor() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM entries WHERE id=?", (appended,)).fetchone()[0] == 1
        )

    now["value"] = "2026-07-11T11:00"
    head = run_paused_write(
        "derived_supersede_rows",
        lambda conn: entries_mod.supersede_entry(
            conn,
            name="project-writer-race.md",
            old_entry_id=appended,
            new_content="",
            reason="empty -->\nreplacement -- reason",
            tags=[],
        ),
    )
    raw = files_mod.memory_path("project-writer-race.md").read_text()
    assert "reason: empty &#45;&#45;&gt; replacement &#45;&#45; reason -->" in raw
    assert "reason: empty -->" not in raw
    with fts.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM entries WHERE id=?", (head,)).fetchone()[0] == 1
        content = conn.execute("SELECT content FROM entries WHERE id=?", (head,)).fetchone()[0]
        assert content == ""
        entries_mod.rebuild_index(conn)
        assert (
            conn.execute("SELECT content FROM entries WHERE id=?", (head,)).fetchone()[0] == content
        )


def test_plain_content_shaped_like_provenance_survives_rebuild(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(entries_mod, "_now_iso_minute", lambda: "2026-07-11T10:00")
    literal = "user-authored\n<!-- supersedes: fake-id; reason: literal text -->"
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-literal.md", description="literal", tags=[])
        entry_id = entries_mod.append_entry(
            conn,
            name="project-literal.md",
            content=literal,
            tags=[],
        )
        entries_mod.rebuild_index(conn)
        assert (
            conn.execute("SELECT content FROM entries WHERE id=?", (entry_id,)).fetchone()[0]
            == literal
        )


def test_evomem_supersede_paused_before_derived_rows_matches_rebuild(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = {"value": "2026-07-11T10:00"}
    monkeypatch.setattr(entries_mod, "_now_iso_minute", lambda: now["value"])
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-evo-race.md", description="race", tags=[])
        old = entries_mod.append_entry(
            conn,
            name="project-evo-race.md",
            content="v1",
            tags=[],
        )
    assert backfill.run_backfill().ok
    (ac_root / "config.toml").write_text('[evomem]\nwrite_authority = "evomem"\n')
    now["value"] = "2026-07-11T11:00"

    real_derived = entries_mod.derived_supersede_rows
    canonical_written = threading.Event()
    resume_projection = threading.Event()
    results: list[str] = []
    errors: list[BaseException] = []

    def paused_derived(*args, **kwargs):
        canonical_written.set()
        if not resume_projection.wait(timeout=10):
            raise TimeoutError("timed out waiting to resume evomem derived rows")
        return real_derived(*args, **kwargs)

    def writer() -> None:
        try:
            with fts.cursor() as conn:
                results.append(
                    entries_mod.supersede_entry(
                        conn,
                        name="project-evo-race.md",
                        old_entry_id=old,
                        new_content="v2",
                        reason="evomem race",
                        tags=[],
                    )
                )
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(entries_mod, "derived_supersede_rows", paused_derived)
    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    assert canonical_written.wait(timeout=10)
    try:
        with fts.cursor() as conn:
            entries_mod.rebuild_index(conn)
    finally:
        resume_projection.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert errors == []
    assert len(results) == 1
    head = results[0]
    with fts.cursor() as conn:
        assert conn.execute("SELECT COUNT(*) FROM entries WHERE id=?", (head,)).fetchone()[0] == 1
        assert conn.execute("SELECT content FROM entries WHERE id=?", (head,)).fetchone()[0] == "v2"
        entries_mod.rebuild_index(conn)
        assert conn.execute("SELECT content FROM entries WHERE id=?", (head,)).fetchone()[0] == "v2"
