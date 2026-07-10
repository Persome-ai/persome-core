"""Edge audit follows Activity source columns instead of assuming intents."""

from __future__ import annotations

from persome.evomem import edge_audit
from persome.store import entries as entries_store
from persome.store import fts
from persome.store import relation_edges as edges


def test_entry_activity_edge_audits_against_durable_entry(ac_root) -> None:
    with fts.cursor() as conn:
        entries_store.create_file(
            conn,
            name="event-2026-07-10.md",
            description="Synthetic activity",
            tags=["event"],
        )
        entry_id = entries_store.append_entry(
            conn,
            name="event-2026-07-10.md",
            content="Reviewed the Persome runtime architecture.",
            tags=["work"],
        )
        edge_id = edges.add_edge(
            conn,
            src_identity="self",
            dst_identity=f"event:entry:{entry_id}",
            predicate="participates_in",
            src_kind="self",
            dst_kind="event",
            provenance="inferred",
            confidence=0.9,
            quote="Reviewed the Persome runtime architecture.",
            source_kind="entry",
            source_id=entry_id,
            source_receipt=f"⟨{entry_id}:event-2026-07-10.md⟩",
        )
        row = conn.execute("SELECT * FROM relation_edges WHERE edge_id=?", (edge_id,)).fetchone()
        verdict = edge_audit.audit_edge(conn, row)
    assert verdict.verdict == "valid"
    assert verdict.checks["source_exists"] is True
    assert verdict.checks["quote_traceable"] is True


def test_missing_entry_source_is_structural_hallucination(ac_root) -> None:
    with fts.cursor() as conn:
        edge_id = edges.add_edge(
            conn,
            src_identity="self",
            dst_identity="event:entry:missing",
            predicate="participates_in",
            src_kind="self",
            dst_kind="event",
            provenance="inferred",
            confidence=0.9,
            quote="Missing synthetic evidence.",
            source_kind="entry",
            source_id="missing",
            source_receipt="⟨missing:event-2026-07-10.md⟩",
        )
        row = conn.execute("SELECT * FROM relation_edges WHERE edge_id=?", (edge_id,)).fetchone()
        verdict = edge_audit.audit_edge(conn, row)
    assert verdict.verdict == "structural_hallucination"
    assert verdict.checks["source_exists"] is False
