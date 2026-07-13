"""Owner identity evidence accumulates safely into the reserved self identity."""

from __future__ import annotations

from types import SimpleNamespace

from persome.evomem import owner_identity
from persome.evomem.engine import EvoMemory
from persome.evomem.models import MemoryLayer
from persome.store import fts
from persome.store import owner_aliases as alias_store


def _cfg():
    return SimpleNamespace(memory_delta=SimpleNamespace(owner_aliases=[]))


def test_owned_account_requires_two_independent_sessions(ac_root) -> None:
    with fts.cursor() as conn:
        first = owner_identity.record_candidate(
            conn,
            alias="Casey-Example",
            session_id="session-1",
            source_kind=alias_store.SOURCE_OWNED_ACCOUNT,
            quote="Opened the user's own GitHub account Casey-Example",
            confidence=0.91,
        )
        duplicate = owner_identity.record_candidate(
            conn,
            alias="Casey-Example",
            session_id="session-1",
            source_kind=alias_store.SOURCE_OWNED_ACCOUNT,
            quote="Opened the user's own GitHub account Casey-Example",
            confidence=0.94,
        )
        assert first is not None and first.status == alias_store.STATUS_PENDING
        assert duplicate is not None and duplicate.evidence_count == 1

        second = owner_identity.record_candidate(
            conn,
            alias="Casey-Example",
            session_id="session-2",
            source_kind=alias_store.SOURCE_OWNED_ACCOUNT,
            quote="Returned to own repositories for Casey-Example",
            confidence=0.9,
        )

    assert second is not None and second.status == alias_store.STATUS_ACTIVE
    assert second.evidence_count == 2 and second.activated_now
    assert owner_identity.active_aliases(_cfg()) == ["Casey-Example"]


def test_explicit_authored_identity_promotes_immediately(ac_root) -> None:
    with fts.cursor() as conn:
        state = owner_identity.record_candidate(
            conn,
            alias="\u793a\u4f8b\u7532",
            session_id="session-explicit",
            source_kind=alias_store.SOURCE_EXPLICIT_SELF,
            quote="\u6211\u53eb\u793a\u4f8b\u7532",
            confidence=0.96,
        )

    assert state is not None and state.status == alias_store.STATUS_ACTIVE
    assert state.evidence_count == 1


def test_explicit_kind_does_not_promote_unrelated_first_person_sentence(ac_root) -> None:
    with fts.cursor() as conn:
        state = owner_identity.record_candidate(
            conn,
            alias="Kevin",
            session_id="session-ambiguous",
            source_kind=alias_store.SOURCE_EXPLICIT_SELF,
            quote="I am reviewing the launch plan with Kevin",
            confidence=0.99,
        )

    assert state is not None and state.status == alias_store.STATUS_PENDING
    assert not alias_store._explicit_self_quote("Alex", "I am Alexander")
    assert not alias_store._explicit_self_quote("Alex", "I am Alex's manager")


def test_rejected_alias_does_not_reactivate_from_inference(ac_root) -> None:
    with fts.cursor() as conn:
        owner_identity.reject_alias(conn, "Kevin")
        state = owner_identity.record_candidate(
            conn,
            alias="Kevin",
            session_id="session-1",
            source_kind=alias_store.SOURCE_OWNED_ACCOUNT,
            quote="own account Kevin",
            confidence=0.99,
        )

    assert state is not None and state.status == alias_store.STATUS_REJECTED
    assert "Kevin" not in owner_identity.reserved_aliases(_cfg())


def test_stale_pending_alias_no_longer_blocks_person_graph(ac_root) -> None:
    with fts.cursor() as conn:
        owner_identity.record_candidate(
            conn,
            alias="Kevin",
            session_id="session-1",
            source_kind=alias_store.SOURCE_OWNED_ACCOUNT,
            quote="own account Kevin",
            confidence=0.9,
        )
        conn.execute(
            "UPDATE owner_aliases SET last_seen_at='2000-01-01T00:00:00+00:00'"
            " WHERE alias_key='kevin'"
        )

    assert "Kevin" not in owner_identity.reserved_aliases(_cfg())


def test_promotion_retires_existing_person_and_derived_schema(ac_root) -> None:
    memory = EvoMemory()
    memory.add_direct(
        "Casey-Example",
        layer=MemoryLayer.L5_KNOWLEDGE,
        file_name="person-casey-example",
        tags="person-entity",
    )
    memory.add_direct(
        "The person iterates on pull requests.",
        layer=MemoryLayer.L6_SCHEMA,
        file_name="schema-person-casey-example",
        tags="schema stable",
    )

    with fts.cursor() as conn:
        for session_id in ("session-1", "session-2"):
            owner_identity.record_candidate(
                conn,
                alias="Casey-Example",
                session_id=session_id,
                source_kind=alias_store.SOURCE_OWNED_ACCOUNT,
                quote="own GitHub account Casey-Example",
                confidence=0.91,
            )
        active = conn.execute(
            "SELECT file_name FROM evo_nodes WHERE is_latest=1 AND status='active'"
            " AND file_name IN ('person-casey-example.md',"
            " 'schema-person-casey-example.md')"
        ).fetchall()
        retired = conn.execute(
            "SELECT COUNT(*) FROM evo_nodes WHERE status='shadow' AND is_latest=0"
            " AND file_name IN ('person-casey-example.md',"
            " 'schema-person-casey-example.md')"
        ).fetchone()[0]

    assert active == [] and retired == 2
