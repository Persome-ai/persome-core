"""Tests for the ``relation_edges`` DAO (P0-1 / #427, user-centric graph memory).

Spec §4.2 (predicate closed set + endpoint completeness) and §4.6 (DDL + as-of-T).
Covers: schema, ``add_edge`` defaults + closed-set validation, ``close_edge``
append-only stamping, and ``edges_as_of`` valid-time + status filtering.
"""

from __future__ import annotations

import pytest

from persome.evomem.models import MemoryStatus
from persome.store import fts
from persome.store import relation_edges as edges

# Fixed timestamps → deterministic valid-time assertions (no dependency on now).
T_2026_01 = "2026-01-01T00:00:00+00:00"
T_2026_04 = "2026-04-01T00:00:00+00:00"
T_2026_06 = "2026-06-01T00:00:00+00:00"
T_2026_07 = "2026-07-01T00:00:00+00:00"
T_2025_12 = "2025-12-01T00:00:00+00:00"


def _row(conn, edge_id):
    conn.row_factory = None
    return conn.execute(
        "SELECT edge_id, src_identity, dst_identity, predicate, label, valid_from, "
        "valid_to, provenance, confidence, quote, status, created_at "
        "FROM relation_edges WHERE edge_id = ?",
        (edge_id,),
    ).fetchone()


def test_ensure_schema_idempotent_and_indexes(ac_root) -> None:
    with fts.cursor() as conn:
        edges.ensure_schema(conn)
        edges.ensure_schema(conn)  # second call must not raise
        tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='relation_edges'"
        ).fetchone()
        idx = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='relation_edges'"
            ).fetchall()
        }
    assert tbl is not None
    assert {"ix_edges_src", "ix_edges_dst"} <= idx


def test_add_edge_defaults_shadow_open_and_returns_id(ac_root) -> None:
    with fts.cursor() as conn:
        eid = edges.add_edge(
            conn,
            src_identity="alice",
            dst_identity="persome",
            predicate=edges.Predicate.PARTICIPATES_IN,
            src_kind=edges.EntityKind.PERSON,
            dst_kind=edges.EntityKind.PROJECT,
            provenance="inferred",
            confidence=0.8,
            label="\u8d1f\u8d23",
            quote="alice \u8d1f\u8d23 Persome \u8fd9\u4e2a\u9879\u76ee",
            valid_from=T_2026_01,
        )
        row = _row(conn, eid)
    assert eid and len(eid) >= 8
    # columns: edge_id,src,dst,pred,label,valid_from,valid_to,prov,conf,quote,status,created_at
    assert row[1] == "alice" and row[2] == "persome"
    assert row[3] == "participates_in"
    assert row[5] == T_2026_01
    assert row[6] is None  # valid_to NULL = open
    assert row[10] == MemoryStatus.SHADOW.value  # default inert
    assert row[11]  # created_at stamped


def test_add_edge_accepts_string_predicate_and_kinds(ac_root) -> None:
    with fts.cursor() as conn:
        eid = edges.add_edge(
            conn,
            src_identity="alice",
            dst_identity="bob",
            predicate="reports_to",
            src_kind="person",
            dst_kind="person",
            provenance="user_committed",
            confidence=1.0,
        )
    assert eid


@pytest.mark.parametrize(
    "kwargs",
    [
        # off-set predicate
        dict(predicate="manages", src_kind="person", dst_kind="person"),
        # illegal endpoints for a valid predicate (reports_to is person->person only)
        dict(predicate="reports_to", src_kind="event", dst_kind="person"),
        dict(predicate="participates_in", src_kind="person", dst_kind="person"),
        # off-set entity kind
        dict(predicate="knows", src_kind="robot", dst_kind="person"),
    ],
)
def test_add_edge_rejects_off_table(ac_root, kwargs) -> None:
    with fts.cursor() as conn, pytest.raises(ValueError):
        edges.add_edge(
            conn,
            src_identity="a",
            dst_identity="b",
            provenance="inferred",
            confidence=0.5,
            **kwargs,
        )


@pytest.mark.parametrize(
    "bad",
    [
        dict(provenance="hearsay", confidence=0.5),  # unknown provenance
        dict(provenance="inferred", confidence=1.5),  # confidence out of range
        dict(provenance="inferred", confidence=-0.1),
    ],
)
def test_add_edge_rejects_bad_provenance_or_confidence(ac_root, bad) -> None:
    with fts.cursor() as conn, pytest.raises(ValueError):
        edges.add_edge(
            conn,
            src_identity="a",
            dst_identity="b",
            predicate="knows",
            src_kind="person",
            dst_kind="person",
            **bad,
        )


def test_add_edge_rejects_empty_identity(ac_root) -> None:
    with fts.cursor() as conn, pytest.raises(ValueError):
        edges.add_edge(
            conn,
            src_identity="   ",
            dst_identity="b",
            predicate="knows",
            src_kind="person",
            dst_kind="person",
            provenance="inferred",
            confidence=0.5,
        )


def test_close_edge_stamps_valid_to_once(ac_root) -> None:
    with fts.cursor() as conn:
        eid = edges.add_edge(
            conn,
            src_identity="alice",
            dst_identity="boss",
            predicate="reports_to",
            src_kind="person",
            dst_kind="person",
            provenance="user_committed",
            confidence=0.9,
            valid_from=T_2026_01,
            status=MemoryStatus.ACTIVE,
        )
        created_before = _row(conn, eid)[11]

        assert edges.close_edge(conn, edge_id=eid, at=T_2026_06) is True
        row = _row(conn, eid)
        assert row[6] == T_2026_06  # valid_to stamped
        assert row[11] == created_before  # created_at immutable

        # append-only: a second close (reopen) is refused
        assert edges.close_edge(conn, edge_id=eid, at=T_2026_07) is False
        assert _row(conn, eid)[6] == T_2026_06

        # closing a non-existent edge is a no-op False
        assert edges.close_edge(conn, edge_id="nope") is False


def test_edges_as_of_valid_time_and_status(ac_root) -> None:
    with fts.cursor() as conn:
        active = edges.add_edge(
            conn,
            src_identity="alice",
            dst_identity="boss",
            predicate="reports_to",
            src_kind="person",
            dst_kind="person",
            provenance="user_committed",
            confidence=0.9,
            valid_from=T_2026_01,
            status=MemoryStatus.ACTIVE,
        )
        # a shadow edge on the same identity must never surface via the active traversal
        edges.add_edge(
            conn,
            src_identity="alice",
            dst_identity="persome",
            predicate="participates_in",
            src_kind="person",
            dst_kind="project",
            provenance="inferred",
            confidence=0.7,
            valid_from=T_2026_01,
            status=MemoryStatus.SHADOW,
        )

        # open + active → visible now, and at a T after valid_from
        assert {r["edge_id"] for r in edges.edges_as_of(conn, ["alice"], as_of=T_2026_07)} == {
            active
        }
        # before valid_from → not yet valid
        assert edges.edges_as_of(conn, ["alice"], as_of=T_2025_12) == []
        # shadow visible only when explicitly asked for shadow
        shadow = edges.edges_as_of(conn, ["alice"], as_of=T_2026_07, status=MemoryStatus.SHADOW)
        assert len(shadow) == 1 and shadow[0]["status"] == "shadow"

        # close the active edge at 2026-06 → invisible after, visible within the interval
        edges.close_edge(conn, edge_id=active, at=T_2026_06)
        assert edges.edges_as_of(conn, ["alice"], as_of=T_2026_07) == []
        assert {r["edge_id"] for r in edges.edges_as_of(conn, ["alice"], as_of=T_2026_04)} == {
            active
        }


def test_edges_as_of_matches_src_or_dst(ac_root) -> None:
    with fts.cursor() as conn:
        eid = edges.add_edge(
            conn,
            src_identity="alice",
            dst_identity="bob",
            predicate="knows",
            src_kind="person",
            dst_kind="person",
            provenance="inferred",
            confidence=0.6,
            valid_from=T_2026_01,
            status=MemoryStatus.ACTIVE,
        )
        assert {r["edge_id"] for r in edges.edges_as_of(conn, ["bob"], as_of=T_2026_07)} == {eid}
        assert edges.edges_as_of(conn, ["carol"], as_of=T_2026_07) == []
        assert edges.edges_as_of(conn, [], as_of=T_2026_07) == []


def test_reinforce_edge_monotone_and_refuses_closed(ac_root) -> None:
    with fts.cursor() as conn:
        eid = edges.add_edge(
            conn,
            src_identity="alice",
            dst_identity="bob",
            predicate="knows",
            src_kind="person",
            dst_kind="person",
            provenance="inferred",
            confidence=0.5,
        )
        row = _row(conn, eid)
        created_before = row[11]

        # grow: observations 1 -> 3, confidence ratchets up via MAX
        assert edges.reinforce_edge(conn, edge_id=eid, observations=3, confidence=0.8) is True
        conn.row_factory = None
        obs, conf = conn.execute(
            "SELECT observations, confidence FROM relation_edges WHERE edge_id=?", (eid,)
        ).fetchone()
        assert obs == 3 and conf == 0.8
        # idempotent: same or lower count is a no-op (returns False, nothing changes)
        assert edges.reinforce_edge(conn, edge_id=eid, observations=3) is False
        assert edges.reinforce_edge(conn, edge_id=eid, observations=2) is False
        # confidence never ratchets DOWN
        assert edges.reinforce_edge(conn, edge_id=eid, observations=4, confidence=0.1) is True
        conf2 = conn.execute(
            "SELECT confidence FROM relation_edges WHERE edge_id=?", (eid,)
        ).fetchone()[0]
        assert conf2 == 0.8
        # created_at (transaction time) untouched
        assert _row(conn, eid)[11] == created_before
        # closed edges refuse reinforcement
        edges.close_edge(conn, edge_id=eid, at=T_2026_06)
        assert edges.reinforce_edge(conn, edge_id=eid, observations=99) is False


def test_reinforce_edge_confidence_ratchets_without_observations_growth(ac_root) -> None:
    """#453: confidence must ratchet up even when observations stays put.

    The LLM `reports_to` pass (and the activity pass) always call `reinforce_edge` with the
    default `observations=1`, so a stronger later run — same edge, same observations, higher
    confidence — must still lift confidence. Previously the MAX rode the `observations < ?`
    UPDATE and never fired, freezing confidence at its first-seen value.
    """
    with fts.cursor() as conn:
        eid = edges.add_edge(
            conn,
            src_identity="carol",
            dst_identity="dave",
            predicate="reports_to",
            src_kind="person",
            dst_kind="person",
            provenance="inferred",
            confidence=0.72,
            observations=1,
        )

        # observations UNCHANGED (1 → 1) but a higher confidence → must ratchet up + return True.
        assert edges.reinforce_edge(conn, edge_id=eid, observations=1, confidence=0.85) is True
        conn.row_factory = None
        obs, conf = conn.execute(
            "SELECT observations, confidence FROM relation_edges WHERE edge_id=?", (eid,)
        ).fetchone()
        assert obs == 1, f"observations must stay pinned; got {obs}"
        assert conf == 0.85, f"confidence must ratchet up to MAX; got {conf}"

        # same observations + a LOWER confidence → no-op (never ratchets down), returns False.
        assert edges.reinforce_edge(conn, edge_id=eid, observations=1, confidence=0.5) is False
        conf2 = conn.execute(
            "SELECT confidence FROM relation_edges WHERE edge_id=?", (eid,)
        ).fetchone()[0]
        assert conf2 == 0.85

        # same observations + EQUAL confidence → no strength change, returns False.
        assert edges.reinforce_edge(conn, edge_id=eid, observations=1, confidence=0.85) is False


def test_ensure_schema_backfills_new_columns_on_old_db(ac_root) -> None:
    """A DB created by the first shipped schema (no observations column) gets ALTERed."""
    with fts.cursor() as conn:
        conn.executescript(
            """
            CREATE TABLE relation_edges (
                edge_id TEXT PRIMARY KEY, src_identity TEXT NOT NULL,
                dst_identity TEXT NOT NULL, predicate TEXT NOT NULL, label TEXT,
                valid_from TEXT NOT NULL, valid_to TEXT, provenance TEXT NOT NULL,
                confidence REAL NOT NULL, quote TEXT, status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO relation_edges VALUES ('e1','a','b','knows',NULL,'2026-01-01',NULL,"
            "'inferred',0.5,NULL,'shadow','2026-01-01')"
        )
        edges.ensure_schema(conn)  # must ALTER, not crash
        conn.row_factory = None
        obs = conn.execute("SELECT observations FROM relation_edges WHERE edge_id='e1'").fetchone()[
            0
        ]
        assert obs == 1  # backfilled default
        # and reinforcement works on the migrated row
        assert edges.reinforce_edge(conn, edge_id="e1", observations=2) is True


def _seed_knows(conn, n, *, src="self", obs=5):
    ids = []
    for i in range(n):
        eid = edges.add_edge(
            conn,
            src_identity=src,
            dst_identity=f"p{i}-{src}",
            predicate=edges.Predicate.KNOWS,
            src_kind=edges.EntityKind.SELF if src == "self" else edges.EntityKind.PERSON,
            dst_kind=edges.EntityKind.PERSON,
            provenance="inferred",
            confidence=0.9,
        )
        conn.execute("UPDATE relation_edges SET observations=? WHERE edge_id=?", (obs, eid))
        ids.append(eid)
    return ids


def _statuses(conn):
    conn.row_factory = None
    return {r[0]: r[1] for r in conn.execute("SELECT edge_id, status FROM relation_edges")}


def test_promote_evidence_floor(ac_root) -> None:
    with fts.cursor() as conn:
        edges.ensure_schema(conn)
        weak = _seed_knows(conn, 1, obs=2)
        strong = _seed_knows(conn, 1, src="other", obs=3)
        assert edges.promote_edges(conn, min_observations=3, max_per_identity=10) == 1
        st = _statuses(conn)
    assert st[weak[0]] == "shadow" and st[strong[0]] == "active"


def test_promote_fanout_cap_takes_strongest(ac_root) -> None:
    """Promotion volume IS relation-pool dilution volume (the PR #504 A/B showed
    naive threshold promotion makes retrieval WORSE): per identity only the
    strongest edges spread activation."""
    with fts.cursor() as conn:
        edges.ensure_schema(conn)
        ids = _seed_knows(conn, 5, obs=3)
        conn.execute("UPDATE relation_edges SET observations=99 WHERE edge_id=?", (ids[2],))
        assert edges.promote_edges(conn, min_observations=3, max_per_identity=2) == 2
        st = _statuses(conn)
    assert st[ids[2]] == "active"  # the strongest always makes the cap
    assert sum(1 for v in st.values() if v == "active") == 2


def test_promote_idempotent_and_active_counts_against_cap(ac_root) -> None:
    with fts.cursor() as conn:
        edges.ensure_schema(conn)
        _seed_knows(conn, 3, obs=5)
        assert edges.promote_edges(conn, min_observations=3, max_per_identity=2) == 2
        # second night: the two ACTIVE occupy the cap; the third stays shadow
        assert edges.promote_edges(conn, min_observations=3, max_per_identity=2) == 0
        st = _statuses(conn)
    assert sum(1 for v in st.values() if v == "active") == 2


def test_promote_cap_is_per_source_identity(ac_root) -> None:
    with fts.cursor() as conn:
        edges.ensure_schema(conn)
        _seed_knows(conn, 2, src="self", obs=5)
        _seed_knows(conn, 2, src="\u5f20\u4f1f", obs=5)
        assert edges.promote_edges(conn, min_observations=3, max_per_identity=2) == 4


def test_promote_never_demotes(ac_root) -> None:
    with fts.cursor() as conn:
        edges.ensure_schema(conn)
        ids = _seed_knows(conn, 1, obs=9)
        edges.promote_edges(conn, min_observations=3, max_per_identity=10)
        assert edges.promote_edges(conn, min_observations=100, max_per_identity=10) == 0
        st = _statuses(conn)
    assert st[ids[0]] == "active"


# ── §7-6 graph-projection axes: kinds + polarity persist (were validate-only) ──


def test_kinds_and_polarity_persist(ac_root) -> None:
    with fts.cursor() as conn:
        eid = edges.add_edge(
            conn,
            src_identity="self",
            dst_identity="\u5f20\u4f1f",
            predicate="knows",
            src_kind="self",
            dst_kind="person",
            provenance="inferred",
            confidence=0.9,
        )
        row = conn.execute(
            "SELECT src_kind, dst_kind, polarity FROM relation_edges WHERE edge_id=?", (eid,)
        ).fetchone()
    assert tuple(row) == ("self", "person", "0")


def test_polarity_closed_set(ac_root) -> None:
    with fts.cursor() as conn, pytest.raises(ValueError, match="polarity"):
        edges.add_edge(
            conn,
            src_identity="self",
            dst_identity="\u5f20\u4f1f",
            predicate="knows",
            src_kind="self",
            dst_kind="person",
            provenance="inferred",
            confidence=0.9,
            polarity="positive",
        )


def test_ensure_schema_backfills_axis_columns(ac_root) -> None:
    import sqlite3

    # simulate a pre-axis DB: the originally-shipped CREATE (no extra columns)
    c = sqlite3.connect(":memory:")
    c.execute(
        """
        CREATE TABLE relation_edges (
            edge_id TEXT PRIMARY KEY, src_identity TEXT NOT NULL,
            dst_identity TEXT NOT NULL, predicate TEXT NOT NULL, label TEXT,
            valid_from TEXT NOT NULL, valid_to TEXT, provenance TEXT NOT NULL,
            confidence REAL NOT NULL, quote TEXT, status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    edges.ensure_schema(c)
    cols = {r[1] for r in c.execute("PRAGMA table_info(relation_edges)")}
    assert {
        "src_kind",
        "dst_kind",
        "polarity",
        "observations",
        "recall_count",
        "source_kind",
        "source_id",
        "source_receipt",
    } <= cols


def test_source_receipt_round_trips_as_one_contract(ac_root) -> None:
    with fts.cursor() as conn:
        eid = edges.add_edge(
            conn,
            src_identity="self",
            dst_identity="persome",
            predicate="participates_in",
            src_kind="self",
            dst_kind="project",
            provenance="inferred",
            confidence=0.9,
            source_kind="session",
            source_id="event:session:synthetic-1",
            source_receipt="⟨event:session:synthetic-1:fixtures/session-1.json⟩",
        )
        row = conn.execute(
            "SELECT source_kind, source_id, source_receipt FROM relation_edges WHERE edge_id=?",
            (eid,),
        ).fetchone()
    assert tuple(row) == (
        "session",
        "event:session:synthetic-1",
        "⟨event:session:synthetic-1:fixtures/session-1.json⟩",
    )


def test_source_receipt_rejects_partial_contract(ac_root) -> None:
    with fts.cursor() as conn, pytest.raises(ValueError, match="must be supplied together"):
        edges.add_edge(
            conn,
            src_identity="self",
            dst_identity="persome",
            predicate="participates_in",
            src_kind="self",
            dst_kind="project",
            provenance="inferred",
            confidence=0.9,
            source_kind="session",
        )


def _edge_with_quote(conn, quote, dst="\u5f20\u4f1f"):
    return edges.add_edge(
        conn,
        src_identity="self",
        dst_identity=dst,
        predicate="knows",
        src_kind="self",
        dst_kind="person",
        provenance="inferred",
        confidence=0.9,
        quote=quote,
    )


def test_close_edges_quoted_in_closes_matching_open_edges(ac_root) -> None:
    with fts.cursor() as conn:
        hit = _edge_with_quote(conn, "\u5f20\u4f1f\u662f\u9879\u76ee\u8d1f\u8d23\u4eba")
        miss = _edge_with_quote(
            conn, "\u674e\u56db\u63d0\u4ea4\u4e86\u8bc4\u5ba1", dst="\u674e\u56db"
        )
        closed = edges.close_edges_quoted_in(
            conn,
            "\u65e7\u4e8b\u5b9e\uff1a\u5f20\u4f1f\u662f\u9879\u76ee\u8d1f\u8d23\u4eba\uff08\u5df2\u88ab\u88c1\u6389\uff09",
        )
        assert closed == [hit]
        rows = {r[0]: r[1] for r in conn.execute("SELECT edge_id, valid_to FROM relation_edges")}
        assert rows[hit] is not None and rows[miss] is None
        # idempotent: the already-closed edge no longer matches valid_to IS NULL
        assert (
            edges.close_edges_quoted_in(conn, "\u5f20\u4f1f\u662f\u9879\u76ee\u8d1f\u8d23\u4eba")
            == []
        )


def test_close_edges_quoted_in_empty_content_closes_nothing(ac_root) -> None:
    with fts.cursor() as conn:
        _edge_with_quote(conn, "\u5f20\u4f1f\u662f\u9879\u76ee\u8d1f\u8d23\u4eba")
        assert edges.close_edges_quoted_in(conn, "   ") == []
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM relation_edges WHERE valid_to IS NOT NULL"
            ).fetchone()[0]
            == 0
        )


def test_add_edge_birth_stamps_last_observed_at(ac_root) -> None:
    """last_observed_at is born = the first evidence moment (2026-07-03 y-axis
    audit: 108/109 production edges were NULL because only reinforce stamped
    it, starving the continuous temporal sink)."""
    with fts.cursor() as conn:
        eid = edges.add_edge(
            conn,
            src_identity="self",
            dst_identity="\u5f20\u4f1f",
            predicate="knows",
            src_kind="self",
            dst_kind="person",
            provenance="inferred",
            confidence=0.9,
            valid_from="2026-06-20T10:00:00+00:00",
        )
        row = conn.execute(
            "SELECT valid_from, last_observed_at FROM relation_edges WHERE edge_id=?", (eid,)
        ).fetchone()
    assert row[1] == row[0] == "2026-06-20T10:00:00+00:00"


def test_neighbors_include_shadow_walks_shadow_edges(ac_root) -> None:
    """§7-3 gain unlock A: shadow edges join traversal only on opt-in."""
    with fts.cursor() as conn:
        edges.add_edge(
            conn,
            src_identity="self",
            dst_identity="\u5f20\u4f1f",
            predicate="knows",
            src_kind="self",
            dst_kind="person",
            provenance="inferred",
            confidence=0.9,
            status="active",
        )
        edges.add_edge(
            conn,
            src_identity="\u5f20\u4f1f",
            dst_identity="Bob",
            predicate="knows",
            src_kind="person",
            dst_kind="person",
            provenance="inferred",
            confidence=0.9,  # default status = shadow
        )
        assert edges.neighbors(conn, ["self"], depth=2) == {"\u5f20\u4f1f"}
        assert edges.neighbors(conn, ["self"], depth=2, include_shadow=True) == {
            "\u5f20\u4f1f",
            "Bob",
        }
