import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from persome.store import entries as entries_mod
from persome.store import files as files_mod
from persome.store import fts, index_md


def test_memory_discovery_rejects_symlinked_external_markdown(
    ac_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside-private.md"
    outside.write_text("EXTERNAL PRIVATE CONTENT", encoding="utf-8")
    skills = ac_root / "memory" / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    leak = skills / "skill-leak.md"
    leak.symlink_to(outside)

    assert leak not in files_mod.list_memory_files()
    with fts.cursor() as conn:
        files, rows = entries_mod.rebuild_index(conn)
        hits = fts.search(conn, query="EXTERNAL")
    assert (files, rows) == (0, 0)
    assert hits == []


def test_memory_discovery_does_not_traverse_symlinked_external_directory(
    ac_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside-skills"
    outside.mkdir()
    (outside / "skill-leak.md").write_text(
        "EXTERNAL DIRECTORY PRIVATE CONTENT",
        encoding="utf-8",
    )
    (ac_root / "memory" / "skills").symlink_to(outside, target_is_directory=True)

    assert files_mod.list_memory_files() == []
    with fts.cursor() as conn:
        files, rows = entries_mod.rebuild_index(conn)
        hits = fts.search(conn, query="EXTERNAL")
    assert (files, rows) == (0, 0)
    assert hits == []


def test_make_id_uniqueness() -> None:
    ids = {entries_mod.make_id("2026-04-21T10:30") for _ in range(200)}
    assert len(ids) == 200


def test_connect_names_recovery_path_on_corrupt_header(ac_root: Path) -> None:
    # A live incident shape: page 1 of index.db overwritten, so every fresh
    # connection failed with a bare "file is not a database" that MCP clients
    # retried verbatim for hours. The probe must convert it into one
    # actionable, still-catchable DatabaseError naming the recovery path.
    db = ac_root / "index.db"
    db.write_bytes(b"\x0d\x00\x00\x00" + b"\x00" * 4092)

    with pytest.raises(fts.CorruptDatabaseError, match="persome start") as raised:
        fts.connect(db)
    assert "persome stop" in str(raised.value)


def test_connect_does_not_claim_startup_recovery_for_external_database(ac_root: Path) -> None:
    db = ac_root / "exports" / "damaged-snapshot.db"
    db.parent.mkdir()
    db.write_bytes(b"\x0d\x00\x00\x00" + b"\x00" * 4092)

    with pytest.raises(fts.CorruptDatabaseError) as raised:
        fts.connect(db)
    message = str(raised.value)
    assert "persome start" not in message
    assert "automatic daemon-start recovery applies only to the live index.db" in message


def test_create_append_search(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name="project-persome.md",
            description="Persome OSS project design",
            tags=["project", "ai"],
        )
        eid1 = entries_mod.append_entry(
            conn,
            name="project-persome.md",
            content="User chose Python CLI + daemon form factor for v1.",
            tags=["project", "decision"],
        )
        eid2 = entries_mod.append_entry(
            conn,
            name="project-persome.md",
            content="User picked uv and pyproject.toml over pip + requirements.txt.",
            tags=["project", "tooling"],
        )

        hits = fts.search(conn, query="daemon", top_k=5)
        hit_ids = {h.id for h in hits}
        assert eid1 in hit_ids

        hits2 = fts.search(conn, query="uv", top_k=5)
        assert any(h.id == eid2 for h in hits2)

        # GLOB path filter
        hits3 = fts.search(conn, query="Python", path_patterns=["project-*.md"], top_k=5)
        assert len(hits3) >= 1


def test_nested_and_top_level_files_with_same_basename_keep_distinct_identities(
    ac_root: Path,
) -> None:
    top_name = "skill-same.md"
    nested_name = "skills/skill-same.md"

    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name=top_name,
            description="Top-level skill",
            tags=["top"],
        )
        entries_mod.create_file(
            conn,
            name=nested_name,
            description="Nested direct skill",
            tags=["nested"],
        )
        top_id = entries_mod.append_entry(
            conn,
            name=top_name,
            content="Top-level behavior",
            tags=["top"],
        )
        nested_id = entries_mod.append_entry(
            conn,
            name=nested_name,
            content="Nested behavior",
            tags=["nested"],
        )
        entries_mod.set_file_status(conn, name=nested_name, status="dormant")

        before = {
            row["path"]: (row["description"], row["status"])
            for row in conn.execute(
                "SELECT path, description, status FROM files ORDER BY path"
            ).fetchall()
        }
        entry_paths = {
            row["id"]: row["path"]
            for row in conn.execute(
                "SELECT id, path FROM entries WHERE id IN (?, ?)",
                (top_id, nested_id),
            ).fetchall()
        }

        assert before == {
            top_name: ("Top-level skill", "active"),
            nested_name: ("Nested direct skill", "dormant"),
        }
        assert entry_paths == {top_id: top_name, nested_id: nested_name}

        assert entries_mod.rebuild_index(conn) == (2, 2)
        after = {
            row["path"]: (row["description"], row["status"])
            for row in conn.execute(
                "SELECT path, description, status FROM files ORDER BY path"
            ).fetchall()
        }
        rebuilt_entry_paths = {
            row["id"]: row["path"]
            for row in conn.execute(
                "SELECT id, path FROM entries WHERE id IN (?, ?)",
                (top_id, nested_id),
            ).fetchall()
        }

    assert after == before
    assert rebuilt_entry_paths == entry_paths


def test_evomem_rebuild_migrates_verified_legacy_nested_skill_shadow(
    ac_root: Path,
) -> None:
    """Upgrade a pre-fix basename shadow without losing the direct source."""
    from persome import config as config_mod
    from persome import paths
    from persome.evomem.models import MemoryLayer, MemoryNode
    from persome.evomem.store import NodeStore

    nested_name = "skills/skill-upgrade.md"
    content = "Prefer explicit proof before changing durable state."
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name=nested_name,
            description="Direct behavioral memory",
            tags=["behavior"],
        )
        entry_id = entries_mod.append_entry(
            conn,
            name=nested_name,
            content=content,
            tags=["observed"],
        )

    # Old shadow.py used Path.name here, erasing the ``skills/`` identity.
    NodeStore().save(
        MemoryNode(
            node_id=entry_id,
            content=content,
            layer=MemoryLayer.L2_FACT,
            file_name="skill-upgrade.md",
            tags="observed",
        )
    )
    NodeStore(user_id="other-user", agent_id="other-agent").save(
        MemoryNode(
            node_id=entry_id,
            content="Other scope remains independent.",
            layer=MemoryLayer.L2_FACT,
            file_name="skill-upgrade.md",
        )
    )
    with fts.cursor() as conn:
        # Complete the on-disk shape produced by the affected versions: both
        # the canonical-node route and the derived source route lost skills/.
        conn.execute(
            "UPDATE entries SET path='skill-upgrade.md' WHERE id=?",
            (entry_id,),
        )
        conn.execute(
            "UPDATE files SET path='skill-upgrade.md' WHERE path=?",
            (nested_name,),
        )
    config_mod.write_default_if_missing()
    paths.atomic_write_private_text(
        paths.config_file(),
        paths.config_file()
        .read_text(encoding="utf-8")
        .replace('write_authority = "markdown"', 'write_authority = "evomem"'),
    )

    with fts.cursor() as conn:
        assert entries_mod.rebuild_index(conn) == (1, 1)
        default_node = conn.execute(
            "SELECT 1 FROM evo_nodes WHERE node_id=? AND user_id='default' AND agent_id='default'",
            (entry_id,),
        ).fetchone()
        other_node = conn.execute(
            "SELECT file_name, content FROM evo_nodes "
            "WHERE node_id=? AND user_id='other-user' AND agent_id='other-agent'",
            (entry_id,),
        ).fetchone()
        rebuilt = conn.execute(
            "SELECT path, content FROM entries WHERE id=?",
            (entry_id,),
        ).fetchall()

    assert default_node is None
    assert other_node is not None and tuple(other_node) == (
        "skill-upgrade.md",
        "Other scope remains independent.",
    )
    assert [tuple(row) for row in rebuilt] == [(nested_name, content)]


@pytest.mark.parametrize("ambiguity", ["source", "content"])
def test_evomem_rebuild_rejects_ambiguous_legacy_nested_skill_shadow(
    ac_root: Path,
    ambiguity: str,
) -> None:
    from persome.evomem.models import MemoryLayer, MemoryNode
    from persome.evomem.store import NodeStore

    nested_name = "skills/skill-ambiguous-upgrade.md"
    content = "Nested source must win only with complete proof."
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name=nested_name, description="d", tags=[])
        entry_id = entries_mod.append_entry(
            conn,
            name=nested_name,
            content=content,
            tags=[],
        )
        if ambiguity == "source":
            entries_mod.create_file(
                conn,
                name="skill-ambiguous-upgrade.md",
                description="Potential top-level source",
                tags=[],
            )
    node_content = content if ambiguity == "source" else "DIVERGENT CANONICAL CONTENT"
    NodeStore().save(
        MemoryNode(
            node_id=entry_id,
            content=node_content,
            layer=MemoryLayer.L2_FACT,
            file_name="skill-ambiguous-upgrade.md",
        )
    )

    # Exercise both proof failures: a basename with a possible top-level source,
    # and a node whose canonical content disagrees with the nested entry.
    with fts.cursor() as conn:
        conn.execute(
            "UPDATE entries SET path='skill-ambiguous-upgrade.md' WHERE id=?",
            (entry_id,),
        )
        with pytest.raises(ValueError, match="source projection differs"):
            entries_mod.rebuild_index(conn, source_authority="evomem")
        node = conn.execute(
            "SELECT file_name, content FROM evo_nodes "
            "WHERE node_id=? AND user_id='default' AND agent_id='default'",
            (entry_id,),
        ).fetchone()

    assert node is not None and tuple(node) == (
        "skill-ambiguous-upgrade.md",
        node_content,
    )


def test_search_multiterm_is_or_ranked_not_and(ac_root: Path) -> None:
    """A natural-language / multi-word query must retrieve an entry that contains
    only SOME of its terms, ranked by bm25 — not require EVERY term (the old
    implicit-AND bug that made /search return nothing for question-shaped queries;
    see spec 2026-06-22-longmemeval-integration-design.md §1). Single-term queries
    are unaffected; this guards the OR fix in _safe_fts_query."""
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="user-pets.md", description="pets", tags=["user"])
        eid = entries_mod.append_entry(
            conn,
            name="user-pets.md",
            content="My dog Mochi is a corgi and loves blueberries.",
            tags=["user"],
        )
        # Filler tokens ("What", "breed", "is") are absent from the entry; under
        # implicit-AND this returned []. Under OR + bm25 it retrieves the entry.
        hits = fts.search(conn, query="What breed is my dog Mochi?", top_k=5)
        assert any(h.id == eid for h in hits), (
            "multi-term query failed to retrieve (AND regression)"
        )


def test_search_handles_apostrophes_and_embedded_quotes(ac_root: Path) -> None:
    """Production FTS escaping must preserve literal punctuation-bearing hints."""
    assert fts._safe_fts_query('\u7528\u6237\u8bf4"\u597d"') == '"\u7528\u6237\u8bf4" OR "\u597d"'
    assert fts._safe_fts_query("meeting 18:00") == '"meeting" OR "18" OR "00"'

    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name="user-preferences.md",
            description="preferences",
            tags=["user"],
        )
        apostrophe = entries_mod.append_entry(
            conn,
            name="user-preferences.md",
            content="User's preference is dark roast.",
            tags=["preference"],
        )
        quoted = entries_mod.append_entry(
            conn,
            name="user-preferences.md",
            content="\u7528\u6237\u8bf4“\u597d”\uff0c\u786e\u8ba4\u4e86 evening \u65b9\u6848\u3002",
            tags=["decision"],
        )

        assert any(hit.id == apostrophe for hit in fts.search(conn, query="User's", top_k=5))
        assert any(
            hit.id == quoted
            for hit in fts.search(conn, query='\u7528\u6237\u8bf4"\u597d"', top_k=5)
        )


def test_supersede_filters_old_by_default(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name="tool-cursor.md", description="Cursor editor", tags=["tool"]
        )
        old = entries_mod.append_entry(
            conn,
            name="tool-cursor.md",
            content="User prefers VSCode as primary editor.",
            tags=["editor"],
        )
        entries_mod.supersede_entry(
            conn,
            name="tool-cursor.md",
            old_entry_id=old,
            new_content="User switched from VSCode to Cursor for AI integration.",
            reason="editor switch",
            tags=["editor"],
        )
        # Default: no superseded
        hits_default = fts.search(conn, query="VSCode", top_k=5)
        assert not any(h.id == old for h in hits_default)
        # With include_superseded: old re-surfaces
        hits_all = fts.search(conn, query="VSCode", top_k=5, include_superseded=True)
        assert any(h.id == old for h in hits_all)


def test_invalid_prefix_rejected(ac_root: Path) -> None:
    with fts.cursor() as conn, pytest.raises(ValueError):
        entries_mod.create_file(conn, name="random-notes.md", description="desc", tags=[])


def test_rebuild_index_round_trip(ac_root: Path) -> None:
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name="user-profile.md", description="identity", tags=["identity"]
        )
        entries_mod.append_entry(
            conn,
            name="user-profile.md",
            content="User is a data scientist.",
            tags=["identity"],
        )
        entries_mod.append_entry(
            conn,
            name="user-profile.md",
            content="User writes a lot of Python.",
            tags=["identity", "skills"],
        )
    with fts.cursor() as conn2:
        file_count, entry_count = entries_mod.rebuild_index(conn2)
        assert file_count == 1
        assert entry_count == 2
        hits = fts.search(conn2, query="Python", top_k=5)
        assert len(hits) >= 1


def test_index_md_rebuild_runs(ac_root: Path) -> None:
    from persome import paths

    with fts.cursor() as conn:
        entries_mod.create_file(
            conn, name="user-profile.md", description="identity", tags=["identity"]
        )
        index_md.rebuild(conn)
    out = (paths.memory_dir() / "index.md").read_text()
    assert "# Memory Index" in out
    assert "user-profile.md" in out


def test_atomic_write_preserves_original_on_replace_failure(tmp_path: Path) -> None:
    """Simulating a crash at the rename step must leave the file intact.

    A SIGKILL between ``write_text``'s first byte and last byte truncates
    the file under the previous code; under ``atomic_write_text`` the
    rename is the only externally-visible step so a failure there leaves
    the original content untouched and any temp file cleaned up.
    """
    target = tmp_path / "memory.md"
    original = "ORIGINAL CONTENT\nline 2\n"
    target.write_text(original)

    real_replace = os.replace
    boom = OSError("simulated rename failure")
    with (
        patch("persome.store.files.os.replace", side_effect=boom),
        pytest.raises(OSError),
    ):
        files_mod.atomic_write_text(target, "NEW CONTENT THAT NEVER LANDS")

    assert target.read_text() == original
    # No leftover .tmp files
    leftovers = [p for p in tmp_path.iterdir() if p.name != "memory.md"]
    assert leftovers == [], f"unexpected leftover files: {leftovers}"

    # Sanity: a normal call still works once we restore replace
    assert os.replace is real_replace
    files_mod.atomic_write_text(target, "NEW CONTENT")
    assert target.read_text() == "NEW CONTENT"


def test_atomic_write_creates_parent_directory(tmp_path: Path) -> None:
    nested = tmp_path / "nested" / "dir" / "file.md"
    files_mod.atomic_write_text(nested, "hello")
    assert nested.read_text() == "hello"


def test_atomic_write_preserves_existing_permissions(tmp_path: Path) -> None:
    """Overwriting must not silently downgrade an existing file's mode.

    ``tempfile.mkstemp`` creates files at 0o600 — without explicit
    chmod the rename would replace a user's 0o644 file with a 0o600
    one, a hidden behavior change from ``Path.write_text``.
    """
    target = tmp_path / "memory.md"
    target.write_text("original")
    target.chmod(0o644)

    files_mod.atomic_write_text(target, "updated")

    assert target.read_text() == "updated"
    assert (target.stat().st_mode & 0o777) == 0o644


def test_atomic_write_round_trip_through_append_entry(ac_root: Path) -> None:
    """End-to-end: append → read returns the new entry, file isn't corrupted."""
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name="topic-rust-async.md",
            description="Rust async patterns",
            tags=["topic"],
        )
        entries_mod.append_entry(
            conn,
            name="topic-rust-async.md",
            content="Tokio's `select!` polls all branches each iteration.",
            tags=["topic", "rust"],
        )
    parsed = files_mod.read_file(files_mod.memory_path("topic-rust-async.md"))
    assert len(parsed.entries) == 1
    assert "Tokio" in parsed.entries[0].body


def test_concurrent_appends_lose_no_entries(ac_root: Path) -> None:
    """N threads appending to the same file must all land.

    Without the per-path lock, ``append_entry`` is read-modify-write:
    each thread reads the same base, appends, and only the last writer's
    version reaches disk — silent data loss with FTS rows pointing at
    entries that don't exist on disk. ``threading.Barrier`` forces every
    thread to enter the critical section as simultaneously as the OS
    will allow, which is what makes the race deterministic enough to
    catch in a unit test.
    """
    n = 30
    name = "topic-load-test.md"

    with fts.cursor() as conn:
        entries_mod.create_file(conn, name=name, description="concurrent appends", tags=["topic"])

    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            barrier.wait()
            with fts.cursor() as conn:
                entries_mod.append_entry(
                    conn,
                    name=name,
                    content=f"entry number {i:02d}",
                    tags=["topic"],
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30.0)

    assert errors == [], f"workers errored: {errors}"

    parsed = files_mod.read_file(files_mod.memory_path(name))
    assert len(parsed.entries) == n, (
        f"expected {n} entries, got {len(parsed.entries)} "
        f"— silent loss indicates the lock is not protecting the read-modify-write"
    )
    bodies = {e.body for e in parsed.entries}
    assert len(bodies) == n, "duplicate or missing entry bodies"
    # File and FTS must agree.
    with fts.cursor() as conn:
        rebuilt_files, rebuilt_entries = entries_mod.rebuild_index(conn)
        assert rebuilt_files == 1
        assert rebuilt_entries == n


def test_concurrent_supersede_then_append_serializes(ac_root: Path) -> None:
    """A supersede + an append on the same file must both land cleanly.

    Without the per-path lock, supersede's two-write read-modify-write
    can interleave with an append in a way that produces a file the
    next ``read_file`` won't even parse. With the lock, both operations
    serialize and the resulting file has 1 superseded original + 1
    superseder + 1 fresh append, all parseable.
    """
    name = "person-bob.md"
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name=name, description="Bob", tags=["person"])
        original = entries_mod.append_entry(
            conn,
            name=name,
            content="Bob is at OpenAI as ML lead.",
            tags=["person"],
        )

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def supersede_worker() -> None:
        try:
            barrier.wait()
            with fts.cursor() as conn:
                entries_mod.supersede_entry(
                    conn,
                    name=name,
                    old_entry_id=original,
                    new_content="Bob moved from OpenAI to Anthropic in 2026-04.",
                    reason="role change",
                    tags=["person"],
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def append_worker() -> None:
        try:
            barrier.wait()
            with fts.cursor() as conn:
                entries_mod.append_entry(
                    conn,
                    name=name,
                    content="Bob's preferred IDE is Cursor.",
                    tags=["person", "preference"],
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=supersede_worker),
        threading.Thread(target=append_worker),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20.0)

    assert errors == [], f"workers errored: {errors}"

    parsed = files_mod.read_file(files_mod.memory_path(name))
    # 1 superseded original + 1 superseder + 1 fresh append = 3 entries.
    assert len(parsed.entries) == 3, (
        f"expected 3 entries, got {len(parsed.entries)} — interleaved writes "
        f"likely lost or corrupted one"
    )
