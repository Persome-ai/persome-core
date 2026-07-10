"Tests for test orphan reaper."

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from persome.evomem.engine import EvoMemory
from persome.store import fts
from persome.writer import delta_apply, orphan_reaper

FUTURE = datetime(2030, 1, 1, tzinfo=UTC)


def _cfg(enabled=True, ttl=30):
    return SimpleNamespace(
        orphan_reaper=SimpleNamespace(enabled=enabled, ttl_days=ttl, max_per_night=200)
    )


def _mint(clean):
    with fts.cursor() as conn:
        delta_apply.apply_delta(conn, None, clean, memory=EvoMemory())


def _active_points(prefix):
    with fts.cursor() as conn:
        conn.row_factory = None
        return {
            r[0]
            for r in conn.execute(
                "SELECT file_name FROM evo_nodes WHERE file_name LIKE ? AND is_latest=1 AND status='active'",
                (f"{prefix}-%",),
            )
        }


def test_orphan_forgotten_connected_kept(ac_root):

    clean = {
        "entities": [
            {"ref": "\u5f20\u4f1f", "kind": "person", "ended": False, "quote": "x"},
            {"ref": "\u674e\u56db", "kind": "person", "ended": False, "quote": "x"},
        ],
        "relations": [
            {
                "src": {"ref": "self"},
                "dst": {"ref": "\u5f20\u4f1f"},
                "predicate": "knows",
                "polarity": "0",
                "ended": False,
                "quote": "\u8ba4\u8bc6\u5f20\u4f1f",
                "confidence": 0.9,
            }
        ],
        "events": [],
        "assertions": [],
    }
    _mint(clean)
    assert _active_points("person") == {"person-\u5f20\u4f1f.md", "person-\u674e\u56db.md"}
    with fts.cursor() as conn:
        r = orphan_reaper.run_orphan_reap(_cfg(), conn, now=FUTURE)
    assert r.reaped == 1
    assert r.reaped_files == ["person-\u674e\u56db.md"]

    assert _active_points("person") == {"person-\u5f20\u4f1f.md"}


def test_young_orphan_kept(ac_root):
    _mint(
        {
            "entities": [{"ref": "\u65b0\u4eba", "kind": "person", "ended": False, "quote": "x"}],
            "relations": [],
            "events": [],
            "assertions": [],
        }
    )

    with fts.cursor() as conn:
        r = orphan_reaper.run_orphan_reap(_cfg(), conn, now=datetime.now(UTC))
    assert r.reaped == 0
    assert _active_points("person") == {"person-\u65b0\u4eba.md"}


def test_events_and_self_not_reaped(ac_root):

    _mint(
        {
            "entities": [],
            "relations": [],
            "events": [
                {
                    "title": "\u5f00\u4e86\u4e2a\u4f1a",
                    "participants": [{"ref": "self"}],
                    "quote": "x",
                    "confidence": 0.9,
                }
            ],
            "assertions": [],
        }
    )
    with fts.cursor() as conn:
        r = orphan_reaper.run_orphan_reap(_cfg(), conn, now=FUTURE)
        conn.row_factory = None

        assert (
            conn.execute(
                "SELECT COUNT(*) FROM relation_edges WHERE dst_identity LIKE 'event:%'"
            ).fetchone()[0]
            == 1
        )
    assert r.reaped == 0


def test_disabled_noop(ac_root):
    _mint(
        {
            "entities": [{"ref": "\u5b64\u513f", "kind": "org", "ended": False, "quote": "x"}],
            "relations": [],
            "events": [],
            "assertions": [],
        }
    )
    with fts.cursor() as conn:
        r = orphan_reaper.run_orphan_reap(_cfg(enabled=False), conn, now=FUTURE)
    assert r.skipped_reason == "disabled" and r.reaped == 0
    assert _active_points("org") == {"org-\u5b64\u513f.md"}


def test_reap_is_soft_receipt_stays(ac_root):

    _mint(
        {
            "entities": [{"ref": "\u8fc7\u5ba2", "kind": "artifact", "ended": False, "quote": "x"}],
            "relations": [],
            "events": [],
            "assertions": [],
        }
    )
    with fts.cursor() as conn:
        orphan_reaper.run_orphan_reap(_cfg(), conn, now=FUTURE)
        conn.row_factory = None

        total = conn.execute(
            "SELECT COUNT(*) FROM evo_nodes WHERE file_name='tool-\u8fc7\u5ba2.md'"
        ).fetchone()[0]
    assert total >= 1
    assert "tool-\u8fc7\u5ba2.md" not in _active_points("tool")


def test_fact_rows_not_treated_as_entities(ac_root):
    cfg = SimpleNamespace(
        memory_delta=SimpleNamespace(apply_assertions=True),
        orphan_reaper=SimpleNamespace(enabled=True, ttl_days=30, max_per_night=200),
    )
    clean = {
        "entities": [{"ref": "\u674e\u56db", "kind": "person", "ended": False, "quote": "x"}],
        "relations": [],
        "events": [],
        "assertions": [
            {
                "subject": {"ref": "\u674e\u56db"},
                "text": "\u674e\u56db\u8d1f\u8d23\u540e\u7aef\u670d\u52a1",
                "quote": "q",
                "confidence": 0.9,
            },
            {
                "subject": {"ref": "\u674e\u56db"},
                "text": "\u674e\u56db\u6628\u5929\u6539\u4e86 inspector.py",
                "quote": "q",
                "confidence": 0.9,
            },
        ],
    }
    with fts.cursor() as conn:
        delta_apply.apply_delta(conn, cfg, clean, memory=EvoMemory())

        conn.row_factory = None
        fact_n = conn.execute(
            "SELECT COUNT(*) FROM evo_nodes WHERE file_name='person-\u674e\u56db.md'"
            " AND is_latest=1 AND status='active' AND tags LIKE 'fact%'"
        ).fetchone()[0]
        assert fact_n == 2
        cands = orphan_reaper.find_orphans(conn, ttl_days=30, now=FUTURE, engaged_keep=2)

    assert {c[2] for c in cands} == {"\u674e\u56db"}
