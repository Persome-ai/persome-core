"Tests for test delta apply."

from __future__ import annotations

from types import SimpleNamespace

import pytest

from persome.evomem.engine import EvoMemory
from persome.store import fts
from persome.store import memory_deltas as deltas_store
from persome.store import relation_edges as edges_store
from persome.writer import delta_apply


def _apply(clean: dict) -> delta_apply.ApplyResult:
    with fts.cursor() as conn:
        return delta_apply.apply_delta(conn, None, clean, memory=EvoMemory())


def _apply_cfg(clean: dict, **flags) -> delta_apply.ApplyResult:
    """apply with a real memory_delta cfg (e.g. apply_assertions=True)."""
    cfg = SimpleNamespace(memory_delta=SimpleNamespace(**flags))
    with fts.cursor() as conn:
        return delta_apply.apply_delta(conn, cfg, clean, memory=EvoMemory())


def _point_files(prefix: str) -> list[str]:
    with fts.cursor() as conn:
        conn.row_factory = None
        return [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT file_name FROM evo_nodes WHERE file_name LIKE ?", (f"{prefix}-%",)
            )
        ]


def test_entities_mint_kind_aware_points(ac_root):
    clean = {
        "entities": [
            {
                "ref": "\u5f20\u4f1f",
                "kind": "person",
                "ended": False,
                "quote": "\u548c\u5f20\u4f1f\u5bf9\u9f50",
            },
            {
                "new_entity": "Acme",
                "kind": "project",
                "ended": False,
                "quote": "Acme \u4e3b\u9879\u76ee",
            },
            {
                "ref": "\u7814\u53d1\u7fa4",
                "kind": "org",
                "ended": False,
                "quote": "\u7814\u53d1\u7fa4\u91cc",
            },
            {"ref": "excalidraw", "kind": "artifact", "ended": False, "quote": "\u7528 excalidraw"},
        ],
        "relations": [],
        "events": [],
        "assertions": [],
    }
    r = _apply(clean)
    assert r.entities_minted == 4

    assert _point_files("person") == ["person-\u5f20\u4f1f.md"]
    assert _point_files("project") == ["project-acme.md"]
    assert _point_files("org") == ["org-\u7814\u53d1\u7fa4.md"]
    assert _point_files("tool") == ["tool-excalidraw.md"]


def test_self_and_bad_kind_skipped(ac_root):
    clean = {
        "entities": [
            {"ref": "self", "kind": "person", "ended": False, "quote": "x"},
            {"ref": "x", "kind": "bogus", "ended": False, "quote": "x"},
            {"kind": "person", "ended": False, "quote": "x"},
        ],
        "relations": [],
        "events": [],
        "assertions": [],
    }
    r = _apply(clean)
    assert r.entities_minted == 0


def test_ended_entity_stamps_valid_until(ac_root):
    clean = {
        "entities": [
            {
                "ref": "\u7814\u53d1\u7fa4",
                "kind": "org",
                "ended": True,
                "quote": "\u9000\u51fa\u7814\u53d1\u7fa4",
            }
        ],
        "relations": [],
        "events": [],
        "assertions": [],
    }
    _apply(clean)
    with fts.cursor() as conn:
        conn.row_factory = None
        rows = conn.execute(
            "SELECT valid_until FROM evo_nodes WHERE file_name = 'org-\u7814\u53d1\u7fa4.md'"
        ).fetchall()
    assert any(row[0] is not None for row in rows)


def test_idempotent_rerun_sees_not_remints(ac_root):
    clean = {
        "entities": [{"ref": "\u5f20\u4f1f", "kind": "person", "ended": False, "quote": "x"}],
        "relations": [],
        "events": [],
        "assertions": [],
    }
    r1 = _apply(clean)
    r2 = _apply(clean)
    assert r1.entities_minted == 1 and r1.entities_seen == 0
    assert r2.entities_minted == 0 and r2.entities_seen == 1


def test_relations_mint_edges_with_polarity(ac_root):
    clean = {
        "entities": [{"ref": "\u5f20\u4f1f", "kind": "person", "ended": False, "quote": "x"}],
        "relations": [
            {
                "src": {"ref": "self"},
                "dst": {"ref": "\u5f20\u4f1f"},
                "predicate": "knows",
                "polarity": "+",
                "ended": False,
                "quote": "\u548c\u5f20\u4f1f\u6109\u5feb\u5408\u4f5c",
                "confidence": 0.9,
            }
        ],
        "events": [],
        "assertions": [],
    }
    r = _apply(clean)
    assert r.edges_new == 1
    with fts.cursor() as conn:
        conn.row_factory = None
        row = conn.execute(
            "SELECT src_identity, dst_identity, predicate, polarity FROM relation_edges"
            " WHERE predicate='knows'"
        ).fetchone()
    assert row == ("self", "\u5f20\u4f1f", "knows", "+")

    with fts.cursor() as conn:
        conn.row_factory = None
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM relation_edges WHERE predicate='engaged_with'"
                " AND dst_identity='\u5f20\u4f1f'"
            ).fetchone()[0]
            == 1
        )


def test_illegal_endpoint_relation_dropped(ac_root):

    clean = {
        "entities": [
            {"ref": "\u5f20\u4f1f", "kind": "person", "ended": False, "quote": "x"},
            {"ref": "\u674e\u56db", "kind": "person", "ended": False, "quote": "x"},
        ],
        "relations": [
            {
                "src": {"ref": "\u5f20\u4f1f"},
                "dst": {"ref": "\u674e\u56db"},
                "predicate": "participates_in",
                "polarity": "0",
                "ended": False,
                "quote": "x",
                "confidence": 0.9,
            }
        ],
        "events": [],
        "assertions": [],
    }
    r = _apply(clean)
    assert r.edges_new == 0
    with fts.cursor() as conn:
        conn.row_factory = None

        assert (
            conn.execute(
                "SELECT COUNT(*) FROM relation_edges WHERE predicate='participates_in'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM relation_edges WHERE predicate='engaged_with'"
            ).fetchone()[0]
            == 2
        )


def test_ended_relation_closes_edge(ac_root):
    clean = {
        "entities": [{"ref": "\u5f20\u4f1f", "kind": "person", "ended": False, "quote": "x"}],
        "relations": [
            {
                "src": {"ref": "self"},
                "dst": {"ref": "\u5f20\u4f1f"},
                "predicate": "reports_to",
                "polarity": "0",
                "ended": True,
                "quote": "\u4e0d\u518d\u5411\u5f20\u4f1f\u6c47\u62a5",
                "confidence": 0.9,
            }
        ],
        "events": [],
        "assertions": [],
    }
    r = _apply(clean)
    assert r.edges_closed == 1
    with fts.cursor() as conn:
        conn.row_factory = None
        vt = conn.execute(
            "SELECT valid_to FROM relation_edges WHERE predicate='reports_to'"
        ).fetchone()[0]
    assert vt is not None


def test_events_mint_activity_point_and_edge(ac_root):
    clean = {
        "entities": [],
        "relations": [],
        "events": [
            {
                "title": "\u5b8c\u6210\u5b63\u5ea6\u5bf9\u8d26",
                "participants": [{"ref": "self"}],
                "quote": "\u5b8c\u6210\u4e86\u5b63\u5ea6\u5bf9\u8d26",
                "confidence": 0.9,
            }
        ],
        "assertions": [],
    }
    r = _apply(clean)
    assert r.events_minted == 1
    with fts.cursor() as conn:
        conn.row_factory = None
        row = conn.execute(
            "SELECT src_identity, dst_identity, predicate FROM relation_edges"
        ).fetchone()
    assert row is not None and row[0] == "self" and row[1].startswith("event:")
    assert row[2] == "participates_in"


def test_empty_and_malformed_fail_open(ac_root):
    assert _apply({}).skipped_reason == "empty"

    r = _apply(
        {"entities": [None, {"kind": "person"}, "garbage"], "relations": [42], "events": [None]}
    )
    assert isinstance(r, delta_apply.ApplyResult)


def test_floor_edge_connects_every_entity_no_orphan(ac_root):
    clean = {
        "entities": [
            {
                "new_entity": "\u817e\u8baf\u6821\u62db",
                "kind": "org",
                "ended": False,
                "quote": "\u770b\u6821\u62db\u9875",
            },
            {
                "new_entity": "\u67d0\u5de5\u5177",
                "kind": "artifact",
                "ended": False,
                "quote": "\u7528\u4e86\u67d0\u5de5\u5177",
            },
        ],
        "relations": [],
        "events": [],
        "assertions": [],
    }
    r = _apply(clean)
    assert r.floor_edges == 2
    with fts.cursor() as conn:
        conn.row_factory = None
        connected = {
            (row[0], row[1])
            for row in conn.execute(
                "SELECT dst_identity, status FROM relation_edges WHERE src_identity='self'"
                " AND predicate='engaged_with'"
            )
        }
    assert connected == {
        ("\u817e\u8baf\u6821\u62db", "active"),
        ("\u67d0\u5de5\u5177", "active"),
    }  # observed Lines


def test_floor_obs_accumulates_across_sessions(ac_root):
    clean = {
        "entities": [
            {"ref": "\u5f20\u4e09", "kind": "person", "ended": False, "quote": "\u548c\u5f20\u4e09"}
        ],
        "relations": [],
        "events": [],
        "assertions": [],
    }
    for _ in range(3):
        _apply(clean)
    with fts.cursor() as conn:
        conn.row_factory = None
        row = conn.execute(
            "SELECT observations FROM relation_edges WHERE predicate='engaged_with'"
            " AND dst_identity='\u5f20\u4e09' AND valid_to IS NULL"
        ).fetchone()
    assert row is not None and row[0] == 3


def test_cooccurrence_relation_accumulates_then_promotes(ac_root):
    clean = {
        "entities": [
            {
                "ref": "\u5f20\u4e09",
                "kind": "person",
                "ended": False,
                "quote": "\u5f20\u4e09\u548c\u674e\u56db",
            },
            {
                "ref": "\u674e\u56db",
                "kind": "person",
                "ended": False,
                "quote": "\u5f20\u4e09\u548c\u674e\u56db",
            },
        ],
        "relations": [
            {
                "src": {"ref": "\u5f20\u4e09"},
                "dst": {"ref": "\u674e\u56db"},
                "predicate": "knows",
                "quote": "",
                "confidence": 0.6,
                "cooccurrence": True,
            }
        ],
        "events": [],
        "assertions": [],
    }
    for _ in range(3):
        _apply(clean)
    with fts.cursor() as conn:
        row = conn.execute(
            "SELECT observations, status FROM relation_edges WHERE predicate='knows'"
        ).fetchone()
        promoted = edges_store.promote_edges(conn)
        status = conn.execute(
            "SELECT status FROM relation_edges WHERE predicate='knows'"
        ).fetchone()[0]
    assert tuple(row) == (3, "shadow")
    assert promoted == 1 and status == "active"


def test_receipt_targets_survive_interleaved_deltas_before_first_mutation(ac_root):
    """A reserved-but-not-mutated delta cannot make the next delta reuse its target."""
    effect_key = "edge:test-interleaved-cooccurrence"
    with fts.cursor() as conn:
        edge_id = edges_store.add_edge(
            conn,
            src_identity="\u5f20\u4e09",
            dst_identity="\u674e\u56db",
            predicate="knows",
            src_kind="person",
            dst_kind="person",
            provenance="inferred",
            confidence=0.6,
            observations=5,
        )
        delta_a = deltas_store.insert(conn, session_id="delta-a", payload={})
        delta_b = deltas_store.insert(conn, session_id="delta-b", payload={})

        # A freezes 6, then crashes before touching the edge. B must observe A's
        # durable reservation and freeze 7 even though the live edge is still 5.
        target_a, found_a, apply_a = deltas_store.reserve_additive_target(
            conn,
            delta_id=delta_a,
            effect_key=effect_key,
            src_identity="\u5f20\u4e09",
            dst_identity="\u674e\u56db",
            predicate="knows",
        )
        target_b, found_b, apply_b = deltas_store.reserve_additive_target(
            conn,
            delta_id=delta_b,
            effect_key=effect_key,
            src_identity="\u674e\u56db",
            dst_identity="\u5f20\u4e09",
            predicate="knows",
        )

        assert (target_a, found_a) == (6, edge_id)
        assert (target_b, found_b) == (7, edge_id)
        assert apply_a and apply_b

        # B mutates first; delayed/retried A uses MAX(6), so it cannot erase or
        # duplicate either observation.
        edges_store.reinforce_edge(
            conn,
            edge_id=edge_id,
            observations=target_b,
            additive=False,
        )
        edges_store.reinforce_edge(
            conn,
            edge_id=edge_id,
            observations=target_a,
            additive=False,
        )
        observations = conn.execute(
            "SELECT observations FROM relation_edges WHERE edge_id=?",
            (edge_id,),
        ).fetchone()[0]
        receipts = conn.execute(
            "SELECT delta_id, target_observations FROM memory_delta_apply_receipts"
            " WHERE effect_key LIKE ? ORDER BY target_observations",
            (f"{effect_key}:generation:%",),
        ).fetchall()

    assert observations == 7
    assert [tuple(row) for row in receipts] == [(delta_a, 6), (delta_b, 7)]


def test_additive_receipt_target_resets_for_reopened_edge_generation(ac_root):
    effect_key = "edge:test-reopened-floor"
    with fts.cursor() as conn:
        first_edge = edges_store.add_edge(
            conn,
            src_identity="self",
            dst_identity="Project A",
            predicate="engaged_with",
            src_kind="self",
            dst_kind="project",
            provenance="inferred",
            confidence=1.0,
            observations=4,
            status="active",
        )
        first_delta = deltas_store.insert(conn, session_id="before-close", payload={})
        first_target, _, apply_first = deltas_store.reserve_additive_target(
            conn,
            delta_id=first_delta,
            effect_key=effect_key,
            src_identity="self",
            dst_identity="Project A",
            predicate="engaged_with",
        )
        assert first_target == 5 and apply_first
        edges_store.reinforce_edge(
            conn,
            edge_id=first_edge,
            observations=first_target,
            additive=False,
        )
        assert edges_store.close_edge(conn, edge_id=first_edge)

        reopened_delta = deltas_store.insert(conn, session_id="after-close", payload={})
        reopened_target, open_edge, apply_reopened = deltas_store.reserve_additive_target(
            conn,
            delta_id=reopened_delta,
            effect_key=effect_key,
            src_identity="self",
            dst_identity="Project A",
            predicate="engaged_with",
        )
        targets = [
            row[0]
            for row in conn.execute(
                "SELECT target_observations FROM memory_delta_apply_receipts"
                " WHERE effect_key LIKE ? ORDER BY delta_id",
                (f"{effect_key}:generation:%",),
            )
        ]

    assert reopened_target == 1 and open_edge is None and apply_reopened
    assert targets == [5, 1]


@pytest.mark.parametrize("mutated_before_close", [False, True])
def test_failed_delta_receipt_never_rebinds_to_reopened_generation(
    ac_root,
    mutated_before_close,
):
    clean = {
        "entities": [
            {
                "ref": "Project A",
                "kind": "project",
                "ended": False,
                "quote": "reviewed Project A",
            }
        ],
        "relations": [],
        "events": [],
        "assertions": [],
    }
    effect_key = delta_apply._additive_effect_key(  # noqa: SLF001
        "self",
        "Project A",
        edges_store.Predicate.ENGAGED_WITH,
    )
    with fts.cursor() as conn:
        old_edge = edges_store.add_edge(
            conn,
            src_identity="self",
            dst_identity="Project A",
            predicate="engaged_with",
            src_kind="self",
            dst_kind="project",
            provenance="inferred",
            confidence=1.0,
            observations=5,
            status="active",
        )
        failed_delta = deltas_store.insert(conn, session_id="failed-old-generation", payload=clean)
        old_target, found_old, should_apply = deltas_store.reserve_additive_target(
            conn,
            delta_id=failed_delta,
            effect_key=effect_key,
            src_identity="self",
            dst_identity="Project A",
            predicate="engaged_with",
        )
        assert (old_target, found_old, should_apply) == (6, old_edge, True)
        if mutated_before_close:
            edges_store.reinforce_edge(
                conn,
                edge_id=old_edge,
                observations=old_target,
                additive=False,
            )
        assert edges_store.close_edge(conn, edge_id=old_edge)

        # A later delta legitimately creates the next validity generation.
        reopened_delta = deltas_store.insert(
            conn,
            session_id="later-reopen",
            payload=clean,
        )
        delta_apply.apply_delta(
            conn,
            None,
            clean,
            memory=EvoMemory(),
            delta_id=reopened_delta,
        )
        reopened_edge = conn.execute(
            "SELECT edge_id, observations FROM relation_edges"
            " WHERE src_identity='self' AND dst_identity='Project A'"
            " AND predicate='engaged_with' AND valid_to IS NULL"
        ).fetchone()
        assert reopened_edge is not None and reopened_edge[1] == 1

        # Retrying the old failed delta must consume its original receipt as a
        # safe no-op. It may not mint a second receipt or touch generation G2.
        retry = delta_apply.apply_delta(
            conn,
            None,
            clean,
            memory=EvoMemory(),
            delta_id=failed_delta,
        )
        reopened_after = conn.execute(
            "SELECT observations FROM relation_edges WHERE edge_id=?",
            (reopened_edge[0],),
        ).fetchone()[0]
        closed_observations = conn.execute(
            "SELECT observations FROM relation_edges WHERE edge_id=?",
            (old_edge,),
        ).fetchone()[0]
        failed_receipts = conn.execute(
            "SELECT COUNT(*) FROM memory_delta_apply_receipts WHERE delta_id=?",
            (failed_delta,),
        ).fetchone()[0]

    assert retry.floor_edges == 0
    assert reopened_after == 1
    assert closed_observations == (6 if mutated_before_close else 5)
    assert failed_receipts == 1


def test_org_nesting_part_of(ac_root):
    clean = {
        "entities": [
            {"new_entity": "\u817e\u8baf", "kind": "org", "ended": False, "quote": "x"},
            {"new_entity": "\u6df7\u5143\u90e8", "kind": "org", "ended": False, "quote": "x"},
        ],
        "relations": [
            {
                "src": {"ref": "\u6df7\u5143\u90e8"},
                "dst": {"ref": "\u817e\u8baf"},
                "predicate": "part_of",
                "polarity": "0",
                "ended": False,
                "quote": "\u6df7\u5143\u90e8\u5c5e\u4e8e\u817e\u8baf",
                "confidence": 0.9,
            }
        ],
        "events": [],
        "assertions": [],
    }
    r = _apply(clean)
    assert r.edges_new == 1
    with fts.cursor() as conn:
        conn.row_factory = None
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM relation_edges WHERE predicate='part_of'"
                " AND src_identity='\u6df7\u5143\u90e8' AND dst_identity='\u817e\u8baf'"
            ).fetchone()[0]
            == 1
        )


def test_classifier_retired_when_apply_enabled(ac_root):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from persome.writer import classifier as classifier_mod

    cfg = SimpleNamespace(
        reducer=SimpleNamespace(enabled=True),
        memory_delta=SimpleNamespace(apply_enabled=True),
    )
    res = classifier_mod.classify_after_reduce(
        cfg,
        session_id="s1",
        event_daily_path="event-2026-07-04.md",
        session_start=datetime(2026, 7, 4, tzinfo=UTC),
        session_end=datetime(2026, 7, 4, 1, tzinfo=UTC),
    )
    assert res.skipped_reason == "classifier retired (delta apply)"
    assert not res.committed


def test_assertions_land_as_fact_entries(ac_root):
    clean = {
        "entities": [{"ref": "\u6e29\u5b50\u58a8", "kind": "person", "ended": False, "quote": "x"}],
        "relations": [],
        "events": [],
        "assertions": [
            {
                "subject": {"ref": "\u6e29\u5b50\u58a8"},
                "text": "\u6e29\u5b50\u58a8\u62ff\u4e86\u817e\u8baf offer",
                "quote": "q",
                "confidence": 0.95,
            },
            {
                "subject": {"ref": "\u6e29\u5b50\u58a8"},
                "text": "\u6e29\u5b50\u58a8\u6539\u4e86 inspector.py",
                "quote": "q",
                "confidence": 0.9,
            },
        ],
    }
    r = _apply_cfg(clean, apply_assertions=True)
    assert r.assertions_minted == 2
    with fts.cursor() as conn:
        conn.row_factory = None
        facts = {
            row[0]
            for row in conn.execute(
                "SELECT content FROM evo_nodes WHERE file_name='person-\u6e29\u5b50\u58a8.md'"
                " AND is_latest=1 AND status='active' AND tags LIKE 'fact%'"
            )
        }
    assert (
        "\u6e29\u5b50\u58a8\u62ff\u4e86\u817e\u8baf offer" in facts
        and "\u6e29\u5b50\u58a8\u6539\u4e86 inspector.py" in facts
    )


def test_assertions_gated_off_by_default(ac_root):
    clean = {
        "entities": [{"ref": "\u738b\u4e94", "kind": "person", "ended": False, "quote": "x"}],
        "relations": [],
        "events": [],
        "assertions": [
            {
                "subject": {"ref": "\u738b\u4e94"},
                "text": "\u738b\u4e94\u8d1f\u8d23X",
                "quote": "q",
                "confidence": 0.9,
            }
        ],
    }
    r = _apply(clean)
    assert r.assertions_minted == 0


def test_assertions_unroutable_subject_skipped(ac_root):
    clean = {
        "entities": [],
        "relations": [],
        "events": [],
        "assertions": [
            {
                "subject": {"new_entity": "\u964c\u751f\u4eba"},
                "text": "\u964c\u751f\u4eba\u505a\u4e86X",
                "quote": "q",
                "confidence": 0.9,
            }
        ],
    }
    r = _apply_cfg(clean, apply_assertions=True)
    assert r.assertions_minted == 0


def test_assertions_idempotent_across_sessions(ac_root):
    clean = {
        "entities": [{"ref": "\u8d75\u516d", "kind": "person", "ended": False, "quote": "x"}],
        "relations": [],
        "events": [],
        "assertions": [
            {
                "subject": {"ref": "\u8d75\u516d"},
                "text": "\u8d75\u516d\u662f\u67b6\u6784\u5e08",
                "quote": "q",
                "confidence": 0.9,
            }
        ],
    }
    _apply_cfg(clean, apply_assertions=True)
    r2 = _apply_cfg(clean, apply_assertions=True)
    assert r2.assertions_minted == 0 and r2.assertions_seen == 1


def test_reproject_entries_from_evomem_feeds_retrieval(ac_root):
    from persome.session.tick import _reproject_entries_from_evomem

    clean = {
        "entities": [{"ref": "\u5f20\u4e09", "kind": "person", "ended": False, "quote": "x"}],
        "relations": [],
        "events": [],
        "assertions": [],
    }
    _apply(clean)
    with fts.cursor() as conn:
        conn.row_factory = None
        before = conn.execute(
            "SELECT count(*) FROM entries WHERE path='person-\u5f20\u4e09.md' AND superseded=0"
        ).fetchone()[0]
    _reproject_entries_from_evomem()
    with fts.cursor() as conn:
        conn.row_factory = None
        after = conn.execute(
            "SELECT count(*) FROM entries WHERE path='person-\u5f20\u4e09.md' AND superseded=0"
        ).fetchone()[0]
    assert before == 0 and after >= 1
