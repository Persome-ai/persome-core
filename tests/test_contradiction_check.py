"""Nightly semantic-contradiction self-check (memory-rebuild spec §4.4).

Deterministic, zero-network (fake llm_call). Covers: the zero-LLM candidate
band pairing, the mark-never-supersede contract (entry_metadata.conflicted +
memory_contradictions row, entry content untouched), metadata preservation,
the pair dedup ledger (clean verdicts silenced too), the config self-gate,
the max_pairs bound, and the human-verdict close path clearing the ⚠ marks.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from persome import config as config_mod
from persome.store import contradictions as contradictions_store
from persome.store import entries as entries_mod
from persome.store import fts
from persome.writer import contradiction_check as check


def _resp(payload: dict):
    msg = SimpleNamespace(content=json.dumps(payload, ensure_ascii=False), tool_calls=[])
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="stop")],
        usage=SimpleNamespace(total_tokens=0),
    )


class CountingJudge:
    def __init__(self, contradictory: bool):
        self.contradictory = contradictory
        self.calls = 0

    def __call__(self, _messages):
        self.calls += 1
        return _resp({"contradictory": self.contradictory, "reason": "测试判定"})


def test_default_llm_adapter_uses_current_call_signature(ac_root, monkeypatch) -> None:
    cfg = _cfg(ac_root)
    seen = {}

    def call_llm(actual_cfg, stage, *, messages):
        seen.update(cfg=actual_cfg, stage=stage, messages=messages)
        return _resp({"contradictory": False, "reason": "clean"})

    monkeypatch.setattr(check.llm_mod, "call_llm", call_llm)
    messages = [{"role": "user", "content": "judge"}]
    check._build_llm_call(cfg)(messages)

    assert seen == {"cfg": cfg, "stage": "contradiction_check", "messages": messages}


# Same-subject, different-claim pair — solidly inside the similarity band.
FACT_A = "张伟目前全职负责支付模块的后端开发工作"
FACT_B = "张伟目前全职负责搜索模块的后端开发工作"
# Unrelated fact — below the band floor against both.
FACT_FAR = "用户习惯在早晨处理邮件"


def _cfg(ac_root, *, enabled=True, max_pairs=10):
    cfg = config_mod.load(ac_root / "config.toml")
    cfg.evomem.contradiction_check_enabled = enabled
    cfg.evomem.contradiction_max_pairs = max_pairs
    return cfg


def _seed(conn, name: str, facts: list[str]) -> list[str]:
    entries_mod.create_file(conn, name=name, description="d", tags=["t"])
    ids = []
    for f in facts:
        entry = entries_mod.append_entry(conn, name=name, content=f, tags=["fact"])
        ids.append(entry.id if hasattr(entry, "id") else entry)
    return ids


def _live_ids(conn, name: str) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT id FROM entries WHERE path=? AND superseded=0 ORDER BY timestamp", (name,)
        )
    ]


class TestCandidatePairing:
    def test_band_pairing_same_file_only(self, ac_root):
        with fts.cursor() as conn:
            _seed(conn, "person-zhangwei.md", [FACT_A, FACT_B, FACT_FAR])
            _seed(conn, "project-pay.md", [FACT_A])  # cross-file: never paired
            pairs = check.find_candidate_pairs(conn, max_pairs=10)
        assert len(pairs) == 1
        assert pairs[0].path == "person-zhangwei.md"
        assert {pairs[0].a_body, pairs[0].b_body} == {FACT_A, FACT_B}

    def test_near_duplicates_excluded(self, ac_root):
        with fts.cursor() as conn:
            _seed(conn, "person-zhangwei.md", [FACT_A, FACT_A + " "])
            pairs = check.find_candidate_pairs(conn, max_pairs=10)
        assert pairs == []  # sim ≈ 1.0 > ceiling — dedup's job, not ours

    def test_event_prefix_never_scanned(self, ac_root):
        with fts.cursor() as conn:
            _seed(conn, "event-2026-07-03.md", [FACT_A, FACT_B])
            pairs = check.find_candidate_pairs(conn, max_pairs=10)
        assert pairs == []

    def test_skip_set_excludes_pair(self, ac_root):
        with fts.cursor() as conn:
            _seed(conn, "person-zhangwei.md", [FACT_A, FACT_B])
            ids = _live_ids(conn, "person-zhangwei.md")
            skip = {contradictions_store.pair_key(*ids)}
            assert check.find_candidate_pairs(conn, max_pairs=10, skip=skip) == []


class TestRunCheck:
    def test_flag_marks_both_and_records_row_never_touches_content(self, ac_root):
        cfg = _cfg(ac_root)
        judge = CountingJudge(contradictory=True)
        with fts.cursor() as conn:
            _seed(conn, "person-zhangwei.md", [FACT_A, FACT_B])
            ids = _live_ids(conn, "person-zhangwei.md")
            res = check.run_contradiction_check(cfg, conn, llm_call=judge)
            assert (res.candidates, res.judged, res.flagged) == (1, 1, 1)
            # both entries ⚠-marked, content untouched, nothing superseded
            for eid in ids:
                assert (fts.get_entry_metadata(conn, eid) or {}).get("conflicted") is True
            assert _live_ids(conn, "person-zhangwei.md") == ids
            rows = contradictions_store.list_rows(conn, status="open")
            assert len(rows) == 1 and rows[0]["reason"] == "测试判定"

    def test_clean_verdict_dismissed_and_unmarked(self, ac_root):
        cfg = _cfg(ac_root)
        judge = CountingJudge(contradictory=False)
        with fts.cursor() as conn:
            _seed(conn, "person-zhangwei.md", [FACT_A, FACT_B])
            res = check.run_contradiction_check(cfg, conn, llm_call=judge)
            assert res.flagged == 0 and res.judged == 1
            for eid in _live_ids(conn, "person-zhangwei.md"):
                meta = fts.get_entry_metadata(conn, eid)
                assert not (meta or {}).get("conflicted")
            # the clean verdict is ledgered as dismissed — silenced, not open
            assert contradictions_store.list_rows(conn, status="open") == []
            assert len(contradictions_store.list_rows(conn, status="dismissed")) == 1

    def test_judged_pair_never_rejudged(self, ac_root):
        cfg = _cfg(ac_root)
        judge = CountingJudge(contradictory=False)
        with fts.cursor() as conn:
            _seed(conn, "person-zhangwei.md", [FACT_A, FACT_B])
            check.run_contradiction_check(cfg, conn, llm_call=judge)
            check.run_contradiction_check(cfg, conn, llm_call=judge)
        assert judge.calls == 1  # second night: pair in the ledger, zero LLM

    def test_disabled_flag_is_noop(self, ac_root):
        cfg = _cfg(ac_root, enabled=False)
        judge = CountingJudge(contradictory=True)
        with fts.cursor() as conn:
            _seed(conn, "person-zhangwei.md", [FACT_A, FACT_B])
            res = check.run_contradiction_check(cfg, conn, llm_call=judge)
        assert judge.calls == 0 and res.candidates == 0

    def test_max_pairs_bounds_llm_calls(self, ac_root):
        cfg = _cfg(ac_root, max_pairs=1)
        judge = CountingJudge(contradictory=False)
        with fts.cursor() as conn:
            _seed(
                conn,
                "person-zhangwei.md",
                [FACT_A, FACT_B, "张伟目前全职负责风控模块的后端开发工作"],
            )
            res = check.run_contradiction_check(cfg, conn, llm_call=judge)
        assert judge.calls == 1 and res.judged == 1

    def test_bad_judge_reply_is_skipped_not_fatal(self, ac_root):
        cfg = _cfg(ac_root)

        def broken(_messages):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="not json at all", tool_calls=[]),
                        finish_reason="stop",
                    )
                ],
                usage=SimpleNamespace(total_tokens=0),
            )

        with fts.cursor() as conn:
            _seed(conn, "person-zhangwei.md", [FACT_A, FACT_B])
            res = check.run_contradiction_check(cfg, conn, llm_call=broken)
            assert res.judged == 0 and res.flagged == 0
            # unjudged pair stays OUT of the ledger — retried next night
            assert contradictions_store.list_rows(conn, status=None) == []

    def test_marking_preserves_existing_confidence(self, ac_root):
        cfg = _cfg(ac_root)
        judge = CountingJudge(contradictory=True)
        with fts.cursor() as conn:
            _seed(conn, "person-zhangwei.md", [FACT_A, FACT_B])
            ids = _live_ids(conn, "person-zhangwei.md")
            fts.set_entry_metadata(conn, ids[0], confidence="low", conflicted=False)
            check.run_contradiction_check(cfg, conn, llm_call=judge)
            meta = fts.get_entry_metadata(conn, ids[0])
        assert meta["conflicted"] is True and meta["confidence"] == "low"


class TestHumanVerdict:
    def _flag(self, cfg, conn):
        _seed(conn, "person-zhangwei.md", [FACT_A, FACT_B])
        check.run_contradiction_check(cfg, conn, llm_call=CountingJudge(True))
        return contradictions_store.list_rows(conn, status="open")[0]

    def test_close_resolved_and_clear_marks(self, ac_root):
        cfg = _cfg(ac_root)
        with fts.cursor() as conn:
            row = self._flag(cfg, conn)
            contradictions_store.close(
                conn, row["pair_key"], status="resolved", keep_id=row["a_id"]
            )
            check.clear_conflicted(conn, row["a_id"], row["b_id"])
            assert contradictions_store.list_rows(conn, status="open") == []
            closed = contradictions_store.list_rows(conn, status="resolved")[0]
            assert closed["keep_id"] == row["a_id"]
            for eid in (row["a_id"], row["b_id"]):
                assert not (fts.get_entry_metadata(conn, eid) or {}).get("conflicted")

    def test_close_unknown_pair_is_none(self, ac_root):
        with fts.cursor() as conn:
            assert contradictions_store.close(conn, "x|y", status="dismissed") is None
