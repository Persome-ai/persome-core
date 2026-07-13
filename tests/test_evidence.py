"""Unified evidence resolution across memory, evomem, and captures."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from persome import evidence as evidence_mod
from persome.evidence import parse_reference, resolve_evidence
from persome.evomem.models import MemoryLayer, MemoryNode
from persome.evomem.store import NodeStore
from persome.store import entries as entries_mod
from persome.store import fts
from persome.store.schema_faces import ensure_schema as ensure_face_schema


def test_parse_receipt_keeps_colons_inside_identifier() -> None:
    receipt = "⟨event:session:synthetic-1:fixtures/session-1.json⟩"
    assert parse_reference(receipt) == (
        "event:session:synthetic-1",
        "fixtures/session-1.json",
        receipt,
    )


def test_memory_receipt_separates_nearby_context_from_direct_sources(ac_root) -> None:
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name="project-persome.md",
            description="Persome project",
            tags=["project"],
        )
        entry_id = entries_mod.append_entry(
            conn,
            name="project-persome.md",
            content="The evidence viewer preserves provenance.",
            tags=["decision"],
            confidence="high",
        )
        timestamp = conn.execute(
            "SELECT timestamp FROM entries WHERE id=?", (entry_id,)
        ).fetchone()[0]
        fts.insert_capture(
            conn,
            id="capture-nearby",
            timestamp=timestamp,
            app_name="Cursor",
            bundle_id="com.test.cursor",
            window_title="evidence.py",
            focused_role="AXTextArea",
            focused_value="",
            visible_text="Implementing an evidence resolver",
            url="",
        )

        result = resolve_evidence(conn, f"⟨{entry_id}:project-persome.md⟩")

    assert result["kind"] == "memory"
    assert result["status"] == "active"
    assert result["label"] == "The evidence viewer preserves provenance."
    assert result["metadata"]["confidence"] == "high"
    assert result["sources"] == []
    assert result["context"][0]["relation"] == "nearby_context"
    assert result["context"][0]["id"] == "capture-nearby"


def test_memory_context_uses_occurred_time_and_canonical_stored_path(ac_root) -> None:
    occurred_at = "2026-06-01T09:00:00+00:00"
    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name="event-2026-06-01.md",
            description="Historical event",
            tags=["event"],
        )
        entry_id = entries_mod.append_entry(
            conn,
            name="event-2026-06-01.md",
            content="Reviewed the evidence at the recorded event time.",
            tags=["work"],
            occurred_at=occurred_at,
        )
        fts.insert_capture(
            conn,
            id="capture-at-event",
            timestamp=occurred_at,
            app_name="Editor",
            bundle_id="com.test.editor",
            window_title="Evidence review",
            focused_role="AXTextArea",
            focused_value="",
            visible_text="Reviewing the evidence",
            url="",
        )
        supplied = f"⟨{entry_id}:wrong-path.md⟩"

        result = resolve_evidence(conn, supplied)

    assert result["reference"] == supplied
    assert result["canonical_reference"] == f"⟨{entry_id}:event-2026-06-01.md⟩"
    assert result["path"] == "event-2026-06-01.md"
    assert [item["id"] for item in result["context"]] == ["capture-at-event"]


def test_point_receipt_exposes_explicit_lineage(ac_root) -> None:
    store = NodeStore()
    store.save(
        MemoryNode(
            node_id="point-source",
            content="The user reviewed source evidence.",
            layer=MemoryLayer.L2_FACT,
            file_name="project-persome.md",
            memory_at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
        )
    )
    store.save(
        MemoryNode(
            node_id="point-derived",
            content="The user values auditable answers.",
            layer=MemoryLayer.L3_SUMMARY,
            file_name="user-preferences.md",
            abstracted_from=["point-source"],
            memory_at=datetime(2026, 7, 2, 9, 0, tzinfo=UTC),
        )
    )

    with fts.cursor() as conn:
        result = resolve_evidence(conn, "⟨point-derived:user-preferences.md⟩")

    assert result["kind"] == "point"
    assert result["summary"] == "The user values auditable answers."
    assert result["sources"] == [
        {
            "relation": "direct_lineage",
            "kind": "point",
            "id": "point-source",
            "reference": "⟨point-source:project-persome.md⟩",
            "label": "The user reviewed source evidence.",
            "timestamp": None,
            "status": None,
            "resolvable": True,
        }
    ]


def test_point_receipt_exposes_human_readable_version_history(ac_root) -> None:
    store = NodeStore()
    store.save(
        MemoryNode(
            node_id="point-old",
            content="The user preferred short answers.",
            layer=MemoryLayer.L2_FACT,
            file_name="user-preferences.md",
        )
    )
    store.save_and_supersede(
        MemoryNode(
            node_id="point-current",
            content="The user now prefers concise answers with evidence.",
            layer=MemoryLayer.L2_FACT,
            file_name="user-preferences.md",
        ),
        old_id="point-old",
    )

    with fts.cursor() as conn:
        current = resolve_evidence(conn, "point-current")
        old = resolve_evidence(conn, "point-old")

    assert current["label"] == "The user now prefers concise answers with evidence."
    assert current["sources"] == []
    assert current["history"][0] == {
        "relation": "previous_version",
        "kind": "point",
        "id": "point-old",
        "reference": "⟨point-old:user-preferences.md⟩",
        "label": "The user preferred short answers.",
        "timestamp": None,
        "status": None,
        "resolvable": True,
    }
    assert old["history"][0]["relation"] == "next_version"
    assert old["history"][0]["label"] == "The user now prefers concise answers with evidence."


def test_aggregate_geometry_reuses_snapshot_point_labels(ac_root, monkeypatch) -> None:
    store = NodeStore()
    store.save(
        MemoryNode(
            node_id="point-readable",
            content="The user checks claims against primary evidence.",
            layer=MemoryLayer.L2_FACT,
            file_name="user-research.md",
        )
    )
    with fts.cursor() as conn:
        ensure_face_schema(conn)
        conn.execute(
            "INSERT INTO schema_faces (face_id, level, signature, members, footprints,"
            " provenance, observations, confidence, status, valid_from, created_at)"
            " VALUES (?, 1, ?, ?, '[]', 'both', 3, 0.91, 'active', ?, ?)",
            (
                "face-evidence",
                "Research decisions remain auditable.",
                json.dumps(["point-readable"]),
                "2026-07-01T00:00:00+00:00",
                "2026-07-01T00:00:00+00:00",
            ),
        )
        monkeypatch.setattr(
            evidence_mod,
            "_reference_label",
            lambda *_args: pytest.fail("snapshot Point labels should not trigger receipt queries"),
        )
        result = resolve_evidence(conn, "face-evidence")

    assert result["kind"] == "face"
    assert result["label"] == "Research decisions remain auditable."
    assert result["sources"][0]["label"] == "The user checks claims against primary evidence."


def test_geometry_resolution_does_not_expand_the_activity_feed(ac_root, monkeypatch) -> None:
    from persome.model.activity_source import ActivitySource

    with fts.cursor() as conn:
        ensure_face_schema(conn)
        conn.execute(
            "INSERT INTO schema_faces (face_id, level, signature, members, footprints,"
            " provenance, observations, confidence, status, valid_from, created_at)"
            " VALUES (?, 1, ?, '[]', '[]', 'both', 3, 0.91, 'active', ?, ?)",
            (
                "face-bounded",
                "Evidence lookup stays bounded.",
                "2026-07-01T00:00:00+00:00",
                "2026-07-01T00:00:00+00:00",
            ),
        )
        monkeypatch.setattr(
            ActivitySource,
            "events",
            lambda *_args, **_kwargs: pytest.fail("geometry lookup must not expand activities"),
        )

        result = resolve_evidence(conn, "face-bounded")

    assert result["kind"] == "face"


def test_unknown_reference_does_not_build_the_full_model(ac_root, monkeypatch) -> None:
    from persome.model import snapshot as snapshot_mod

    monkeypatch.setattr(
        snapshot_mod,
        "build_snapshot",
        lambda *_args, **_kwargs: pytest.fail("unknown refs must not build the model snapshot"),
    )

    with fts.cursor() as conn:
        result = resolve_evidence(conn, "not-a-model-id")

    assert result["status"] == "missing"


def test_activity_resolution_uses_exact_lookup(ac_root, monkeypatch) -> None:
    from persome.model.activity_source import ActivitySource

    with fts.cursor() as conn:
        entries_mod.create_file(
            conn,
            name="event-2026-07-01.md",
            description="Historical activity",
            tags=["event"],
        )
        entry_id = entries_mod.append_entry(
            conn,
            name="event-2026-07-01.md",
            content="Reviewed retained evidence.",
            tags=["work"],
        )
        monkeypatch.setattr(
            ActivitySource,
            "events",
            lambda *_args, **_kwargs: pytest.fail("exact lookup must not build the feed"),
        )

        result = resolve_evidence(conn, f"event:entry:{entry_id}")

    assert result["kind"] == "activity"
    assert result["id"] == f"event:entry:{entry_id}"
    assert result["sources"][0]["id"] == entry_id


def test_capture_resolution_labels_expired_raw_payload_as_metadata_only(ac_root) -> None:
    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id="capture-old",
            timestamp="2026-07-01T09:00:00+00:00",
            app_name="Safari",
            bundle_id="com.apple.Safari",
            window_title="Persome docs",
            focused_role="AXWebArea",
            focused_value="",
            visible_text="Receipts are stable evidence handles.",
            url="https://example.test/docs",
        )
        result = resolve_evidence(conn, "capture-old")

    assert result["kind"] == "capture"
    assert result["status"] == "metadata_only"
    assert result["metadata"]["provenance"] == "observed"
    assert result["metadata"]["raw_capture_available"] is False


def test_capture_resolution_never_checks_outside_capture_buffer(ac_root, monkeypatch) -> None:
    hostile_id = "/tmp/persome-evidence-probe"
    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id=hostile_id,
            timestamp="2026-07-01T09:00:00+00:00",
            app_name="Imported",
            bundle_id="com.test.imported",
            window_title="Unexpected capture id",
            focused_role="AXTextArea",
            focused_value="",
            visible_text="Imported capture metadata",
            url="",
        )
        monkeypatch.setattr(
            Path,
            "lstat",
            lambda *_args, **_kwargs: pytest.fail("hostile IDs must not reach the filesystem"),
        )

        result = resolve_evidence(conn, hostile_id)

    assert result["kind"] == "capture"
    assert result["status"] == "metadata_only"


def test_capture_resolution_does_not_follow_buffer_symlinks(ac_root) -> None:
    outside = ac_root.parent / "outside-capture.json"
    outside.write_text("{}", encoding="utf-8")
    (ac_root / "capture-buffer" / "linked-capture.json").symlink_to(outside)
    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id="linked-capture",
            timestamp="2026-07-01T09:00:00+00:00",
            app_name="Imported",
            bundle_id="com.test.imported",
            window_title="Symlinked capture",
            focused_role="AXTextArea",
            focused_value="",
            visible_text="Imported capture metadata",
            url="",
        )

        result = resolve_evidence(conn, "linked-capture")

    assert result["status"] == "metadata_only"
    assert result["metadata"]["raw_capture_available"] is False


def test_missing_receipt_is_preserved_without_fabricating_evidence(ac_root) -> None:
    receipt = "⟨missing-id:project-missing.md⟩"
    with fts.cursor() as conn:
        result = resolve_evidence(conn, receipt)

    assert result["kind"] == "unknown"
    assert result["status"] == "missing"
    assert result["canonical_reference"] == receipt
    assert result["sources"] == []
    assert result["context"] == []


def test_mcp_registers_the_unified_evidence_resolver(ac_root) -> None:
    from persome.mcp import server as mcp_server

    store = NodeStore()
    store.save(
        MemoryNode(
            node_id="point-mcp",
            content="Agents can inspect this point's evidence.",
            layer=MemoryLayer.L2_FACT,
            file_name="project-persome.md",
        )
    )
    server = mcp_server.build_server(auth_enabled=False)
    resolve = server._tool_manager._tools["resolve_evidence"].fn

    result = json.loads(resolve("⟨point-mcp:project-persome.md⟩"))

    assert result["kind"] == "point"
    assert result["id"] == "point-mcp"
