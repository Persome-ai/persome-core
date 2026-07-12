"""Unified evidence resolution across memory, evomem, and captures."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from persome.evidence import parse_reference, resolve_evidence
from persome.evomem.models import MemoryLayer, MemoryNode
from persome.evomem.store import NodeStore
from persome.store import entries as entries_mod
from persome.store import fts


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
    assert result["metadata"]["confidence"] == "high"
    assert result["sources"] == []
    assert result["context"][0]["relation"] == "nearby_context"
    assert result["context"][0]["id"] == "capture-nearby"


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
            "label": "Earlier evidence used to derive this point",
            "timestamp": None,
            "status": None,
            "resolvable": True,
        }
    ]


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
