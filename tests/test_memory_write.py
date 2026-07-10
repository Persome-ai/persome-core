"""Agent-Native Persome Phase 3 — durable memory write-back (`remember`).

Verifies the write funnels through the canonical writer, force-injects the `source:agent-run`
provenance tag (+ optional `run:<id>`), and that the entry is then searchable.
"""

from __future__ import annotations

import pytest

from persome.mcp import memory_write
from persome.store import files as files_mod
from persome.store import fts


def test_remember_creates_file_and_tags_provenance(ac_root) -> None:
    with fts.cursor() as conn:
        out = memory_write.remember(conn, content="user prefers tabs over spaces")
    assert out["file"] == memory_write.AGENT_FINDINGS_FILE
    assert out["entry_id"]
    assert memory_write.PROVENANCE_TAG in out["tags"]
    # The dedicated findings file now exists on disk.
    assert files_mod.memory_path(memory_write.AGENT_FINDINGS_FILE).exists()


def test_remember_injects_run_id_and_caller_tags(ac_root) -> None:
    with fts.cursor() as conn:
        out = memory_write.remember(
            conn,
            content="shipped the auth refactor",
            tags=["project-x", "decision"],
            run_id="run-42",
        )
    assert out["tags"][0] == memory_write.PROVENANCE_TAG  # provenance always first
    assert "run:run-42" in out["tags"]
    assert "project-x" in out["tags"] and "decision" in out["tags"]


def test_remember_is_searchable_closing_the_loop(ac_root) -> None:
    # A finding written by one agent must be retrievable by the next via `search`.
    with fts.cursor() as conn:
        memory_write.remember(conn, content="the staging deploy token rotates every Monday")
    with fts.cursor() as conn:
        hits = fts.search(conn, query="staging deploy token", top_k=5)
    assert any("staging deploy token" in h.content for h in hits)


def test_remember_rejects_empty_content(ac_root) -> None:
    with fts.cursor() as conn, pytest.raises(ValueError):
        memory_write.remember(conn, content="   ")


def test_remember_appends_not_clobbers(ac_root) -> None:
    with fts.cursor() as conn:
        a = memory_write.remember(conn, content="first finding")
    with fts.cursor() as conn:
        b = memory_write.remember(conn, content="second finding")
    assert a["entry_id"] != b["entry_id"]
    # Both entries persist in the one findings file.
    path = files_mod.memory_path(memory_write.AGENT_FINDINGS_FILE)
    text = path.read_text(encoding="utf-8")
    assert "first finding" in text and "second finding" in text
