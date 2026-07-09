"""Recall budget telemetry (ablation report 2026-06-10 §4 建议 落地).

``assemble_background`` records one ``recall_budget_ticks`` row per call:
scope, max_chars, used, per-layer admitted/rejected counts+chars, and the
derived ``squeezed`` flag. Contract under test:

- P0: the telemetry is a pure side-channel — the assembled output is
  byte-for-byte what it was before, and a failed telemetry write never
  affects recall.
- Squeeze detection: a call where a layer's candidate is rejected for lack
  of budget records ``squeezed=1`` with the rejection attributed to the
  right layer; an unsqueezed call records ``squeezed=0``.
- ``stats`` aggregation: totals, squeeze_rate, per-layer sums and
  rejected_share, since/until filtering.
"""

from __future__ import annotations

import json

from persome.intent import recall
from persome.store import entries as entries_mod
from persome.store import fts, recall_budget_ticks


def _seed_fact(conn, content: str = "ProjectX uses DeepSeek for inference") -> None:
    entries_mod.create_file(conn, name="project-x.md", description="project x", tags=["x"])
    entries_mod.append_entry(conn, name="project-x.md", content=content, tags=["x"])


def _ticks(conn) -> list:
    conn.row_factory = __import__("sqlite3").Row
    return conn.execute("SELECT * FROM recall_budget_ticks ORDER BY id").fetchall()


# ---------------------------------------------------------------------------
# recording: unsqueezed / squeezed states
# ---------------------------------------------------------------------------


def test_unsqueezed_call_records_tick_with_squeezed_zero(ac_root):
    with fts.cursor() as conn:
        _seed_fact(conn)
        bundle = recall.assemble_background(conn, scope="session-1", hints=["ProjectX"])
        assert "ProjectX" in bundle
        rows = _ticks(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["scope"] == "session-1"
    assert row["max_chars"] == 1200
    assert row["squeezed"] == 0
    layers = json.loads(row["layers"])
    # the hit lands in the fact layer (project-* prefix), nothing rejected anywhere
    assert layers["fact"]["admitted"] == 1
    assert layers["fact"]["admitted_chars"] > 0
    assert all(b["rejected"] == 0 for b in layers.values())
    # used is exactly the sum of admitted chars across layers (side-channel sums match)
    assert row["used"] == sum(b["admitted_chars"] for b in layers.values())


def test_squeezed_call_attributes_rejection_to_fact_layer(ac_root):
    with fts.cursor() as conn:
        _seed_fact(conn, content="ProjectX " + "深度记忆内容" * 40)  # > tiny budget
        bundle = recall.assemble_background(
            conn, scope="session-2", hints=["ProjectX"], max_chars=10
        )
        rows = _ticks(conn)
    assert bundle == ""  # nothing fit — output contract unchanged
    assert len(rows) == 1
    row = rows[0]
    assert row["squeezed"] == 1
    assert row["used"] == 0
    layers = json.loads(row["layers"])
    assert layers["fact"]["rejected"] == 1
    assert layers["fact"]["rejected_chars"] > 10
    assert layers["fact"]["admitted"] == 0


def test_one_tick_per_call(ac_root):
    with fts.cursor() as conn:
        _seed_fact(conn)
        recall.assemble_background(conn, scope="s", hints=["ProjectX"])
        recall.assemble_background(conn, scope="s", hints=["ProjectX"])
        recall.assemble_background(conn, scope="s", hints=["ProjectX"])
        assert len(_ticks(conn)) == 3


def test_schema_prior_rejection_attributed_to_schema_prior_layer(ac_root):
    with fts.cursor() as conn:
        bundle = recall.assemble_background(
            conn,
            scope="s",
            hints=[],
            schema_prior=["用户偏好极简工具链" * 20],
            max_chars=10,
        )
        rows = _ticks(conn)
    assert bundle == ""
    layers = json.loads(rows[0]["layers"])
    assert layers["schema_prior"]["rejected"] == 1


# ---------------------------------------------------------------------------
# P0: output byte-identical, telemetry write failure never breaks recall
# ---------------------------------------------------------------------------


def test_p0_output_unchanged_when_telemetry_write_fails(ac_root, monkeypatch):
    with fts.cursor() as conn:
        _seed_fact(conn)
        baseline = recall.assemble_background(conn, scope="session-1", hints=["ProjectX"])

    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(recall.recall_budget_ticks, "record_tick", boom)
    with fts.cursor() as conn:
        degraded = recall.assemble_background(conn, scope="session-1", hints=["ProjectX"])
    assert degraded == baseline  # byte-identical output, no exception escaped


def test_budget_counters_do_not_change_admission(ac_root):
    """The side-channel counters never feed back: admit/reject decisions for a
    given sequence of adds are exactly the pure ``used + len <= max`` rule."""
    b = recall._Budget(10)
    assert b.add("12345", layer="fact") is True
    assert b.add("123456", layer="fact") is False  # 5+6 > 10
    assert b.add("12345", layer="trail") is True  # exactly fills
    assert b.add("", layer="scene") is True  # zero-length always fits
    assert b.used == 10
    assert b.squeezed is True
    assert b.layers["fact"] == {
        "admitted": 1,
        "admitted_chars": 5,
        "rejected": 1,
        "rejected_chars": 6,
    }


# ---------------------------------------------------------------------------
# stats aggregation
# ---------------------------------------------------------------------------


def _layers(**overrides) -> dict:
    base = {
        layer: {c: 0 for c in recall_budget_ticks.COUNTERS} for layer in recall_budget_ticks.LAYERS
    }
    for layer, counters in overrides.items():
        base[layer].update(counters)
    return base


def test_stats_aggregates_squeeze_rate_and_layers(ac_root):
    with fts.cursor() as conn:
        recall_budget_ticks.record_tick(
            conn,
            ts="2026-06-10T10:00:00",
            scope="s",
            max_chars=1200,
            used=1100,
            layers=_layers(
                scene={"admitted": 3, "admitted_chars": 900},
                fact={"admitted": 1, "admitted_chars": 200, "rejected": 2, "rejected_chars": 400},
            ),
        )
        recall_budget_ticks.record_tick(
            conn,
            ts="2026-06-10T11:00:00",
            scope="s",
            max_chars=1200,
            used=300,
            layers=_layers(fact={"admitted": 1, "admitted_chars": 300}),
        )
        out = recall_budget_ticks.stats(conn)
    assert out["total_ticks"] == 2
    assert out["squeezed_ticks"] == 1
    assert out["squeeze_rate"] == 0.5
    assert out["by_layer"]["fact"]["rejected"] == 2
    assert out["by_layer"]["fact"]["rejected_chars"] == 400
    assert out["by_layer"]["fact"]["admitted"] == 2
    assert out["by_layer"]["fact"]["squeezed_ticks"] == 1
    assert out["by_layer"]["scene"]["admitted"] == 3
    assert out["by_layer"]["scene"]["squeezed_ticks"] == 0
    assert out["rejected_share"] == {"fact": 1.0}
    assert out["avg_used"] == 700.0
    assert out["avg_max_chars"] == 1200.0


def test_stats_since_until_filtering(ac_root):
    with fts.cursor() as conn:
        for ts, rejected in (
            ("2026-06-10T10:00:00", 1),
            ("2026-06-11T10:00:00", 0),
            ("2026-06-12T10:00:00", 1),
        ):
            recall_budget_ticks.record_tick(
                conn,
                ts=ts,
                scope="s",
                max_chars=1200,
                used=100,
                layers=_layers(fact={"rejected": rejected, "rejected_chars": rejected * 50}),
            )
        full = recall_budget_ticks.stats(conn)
        middle = recall_budget_ticks.stats(conn, since="2026-06-11", until="2026-06-12")
        tail = recall_budget_ticks.stats(conn, since="2026-06-12")
    assert full["total_ticks"] == 3
    assert full["squeezed_ticks"] == 2
    assert middle["total_ticks"] == 1
    assert middle["squeezed_ticks"] == 0
    assert middle["since"] == "2026-06-11"
    assert middle["until"] == "2026-06-12"
    assert tail["total_ticks"] == 1
    assert tail["squeezed_ticks"] == 1
    assert tail["until"] is None


def test_stats_empty_table(ac_root):
    with fts.cursor() as conn:
        out = recall_budget_ticks.stats(conn)
    assert out["total_ticks"] == 0
    assert out["squeeze_rate"] == 0.0
    assert out["rejected_share"] == {}
    assert set(out["by_layer"]) == set(recall_budget_ticks.LAYERS)


def test_record_tick_derives_squeezed_from_layers(ac_root):
    with fts.cursor() as conn:
        recall_budget_ticks.record_tick(
            conn,
            ts="2026-06-10T10:00:00",
            scope="s",
            max_chars=1200,
            used=0,
            layers=_layers(trail={"rejected": 1, "rejected_chars": 60}),
        )
        rows = _ticks(conn)
    assert rows[0]["squeezed"] == 1


def test_prune_keeps_most_recent(ac_root):
    with fts.cursor() as conn:
        for i in range(10):
            recall_budget_ticks.record_tick(
                conn,
                ts=f"2026-06-10T10:00:{i:02d}",
                scope="s",
                max_chars=1200,
                used=0,
                layers=_layers(),
            )
        deleted = recall_budget_ticks.prune(conn, keep=4)
        rows = _ticks(conn)
    assert deleted == 6
    assert len(rows) == 4
    assert rows[0]["ts"] == "2026-06-10T10:00:06"


# ---------------------------------------------------------------------------
# MCP tool surface
# ---------------------------------------------------------------------------


async def test_mcp_recall_budget_stats_tool(ac_root):
    """The MCP tool returns the same aggregate JSON the DAO computes."""
    from persome.mcp.server import build_server

    with fts.cursor() as conn:
        recall_budget_ticks.record_tick(
            conn,
            ts="2026-06-10T10:00:00",
            scope="s",
            max_chars=1200,
            used=1100,
            layers=_layers(fact={"rejected": 1, "rejected_chars": 200}),
        )
    server = build_server()
    result = await server.call_tool("recall_budget_stats", {})
    payload = json.loads(result[0][0].text)
    assert payload["total_ticks"] == 1
    assert payload["squeezed_ticks"] == 1
    assert payload["by_layer"]["fact"]["rejected"] == 1
