"Tests for test compact evomem repair."

from __future__ import annotations

import re

from persome import config as config_mod
from persome.evomem import backfill
from persome.retrieval import associative as recall
from persome.store import entries as entries_mod
from persome.store import files as files_mod
from persome.store import fts
from persome.writer import compact as compact_mod
from persome.writer import llm as llm_mod

_ID_RE = re.compile(r"\{id:\s*([0-9a-zA-Z-]+)\}")

_HINT = "widgetcorpus"
_BODY = (
    "\u7528\u6237\u5728\u505a widget \u9879\u76ee widgetcorpus alpha beta gamma delta epsilon zeta eta theta "
    "iota kappa lambda mu nu xi omicron pi rho sigma tau upsilon phi chi psi omega"
)


def _rewrite_with_new_ids(markdown: str) -> str:
    counter = {"n": 0}

    def _sub(_m: re.Match[str]) -> str:
        counter["n"] += 1
        return f"{{id: 99999999-9999-{counter['n']:06d}}}"

    return _ID_RE.sub(_sub, markdown)


def _seed_and_backfill() -> None:
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-x.md", description="x", tags=["t"])
        entries_mod.append_entry(conn, name="project-x.md", content=_BODY, tags=["t"])
    report = backfill.run_backfill()
    assert report.ok


def _production_recall() -> str:
    with fts.cursor() as conn:
        hits = recall.associative_read(conn, query=_HINT, top_k=10)
        return "\n".join(hit.content for hit in hits)


def _evo_node_ids() -> set[str]:
    with fts.cursor() as conn:
        return {
            r["node_id"] for r in conn.execute("SELECT node_id FROM evo_nodes WHERE is_latest = 1")
        }


def _run_compact_with_id_churn(monkeypatch) -> None:
    original = files_mod.memory_path("project-x.md").read_text()
    rewritten = _rewrite_with_new_ids(original)
    assert "{id: 99999999" in rewritten and original != rewritten
    monkeypatch.setattr(llm_mod, "call_llm", lambda *a, **k: object())
    monkeypatch.setattr(llm_mod, "extract_text", lambda resp: rewritten)
    cfg = config_mod.Config()
    with fts.cursor() as conn:
        fts.set_needs_compact(conn, "project-x.md", True)
        results = compact_mod.run_pending(cfg, conn)
    assert any(r.accepted for r in results), (
        "compact \u5e94\u88ab\u63a5\u53d7\uff08\u4fdd\u7559\u7387 100%\uff09"
    )


def test_compact_repairs_evomem_synchronously(ac_root, monkeypatch) -> None:
    _seed_and_backfill()
    old_ids = _evo_node_ids()
    assert "widget \u9879\u76ee" in _production_recall(), (
        "backfill \u540e production recall \u5e94\u80fd\u770b\u5230\u8bb0\u5fc6"
    )

    _run_compact_with_id_churn(monkeypatch)

    assert "widget \u9879\u76ee" in _production_recall(), (
        "\u540c\u6b65 repair \u540e production recall \u5e94\u7acb\u5373\u6062\u590d\u8bb0\u5fc6"
    )
    new_ids = _evo_node_ids()
    assert new_ids and not (old_ids & new_ids), (
        "restore \u5e94\u6e05\u6389\u6362 id \u524d\u7684\u65e7 head \u5b64\u513f\uff0cevo_nodes \u53ea\u5269 compact \u540e\u7684\u65b0\u94fe\u5934"
    )
