"""End-to-end smoke for the production context-feedback report (the L3 pipeline).

Pins the CROSS-LANGUAGE CONTRACT: the report consumes JSONL lines in the EXACT shape
the app's Swift ``ContextFeedbackLog.append`` writes (keys ``t/outcome/intentID/kind/
importance/urgency/confidence/headline/taskTitle/taskPrompt``; outcomes
``accepted/dismissed/ignored/completed/failed/manual_baseline``). If either side
drifts, the loop silently breaks — this test turns that into a red.

Deterministic, offline, no LLM.
"""

from __future__ import annotations

import json
from pathlib import Path

from persome.feedback import report as fb


def _swift_line(
    outcome: str,
    *,
    importance: float = 0.0,
    urgency: float = 0.0,
    confidence: float = 0.0,
    intent_id: int = 1,
    kind: str = "reminder",
) -> dict:
    """One feedback line in the EXACT shape Swift ContextFeedbackLog.append emits."""
    return {
        "t": "2026-06-21T03:00:00Z",
        "outcome": outcome,
        "intentID": intent_id,
        "kind": kind,
        "importance": importance,
        "urgency": urgency,
        "confidence": confidence,
        "headline": "下午三点和产品过一版 PRD",
        "taskTitle": "Prep PRD review",
        "taskPrompt": "Draft the v1 PRD review notes",
    }


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )


def test_contract_swift_line_is_consumed(tmp_path: Path) -> None:
    """A single line in the exact Swift schema is parsed and counted by importance —
    the load-bearing cross-language contract assertion."""
    _write_jsonl(
        tmp_path / "context-feedback.jsonl",
        [_swift_line("accepted", importance=0.8)],
    )
    rep = fb.build_report(tmp_path)
    assert rep["n_feedback"] == 1
    assert rep["outcomes"].get("accepted") == 1
    # importance 0.8 ≥ 0.6 → counted in the HIGH bucket as an accept.
    assert rep["accept_by_importance"]["high"]["n"] == 1
    assert rep["accrual"]["high_n"] == 1


def test_all_swift_outcomes_parse(tmp_path: Path) -> None:
    """Every outcome the Swift enum can write is recognized (incl. `failed`, which the
    pre-package report omitted from its display)."""
    rows = [_swift_line(o, importance=0.7) for o in fb.ALL_OUTCOMES]
    _write_jsonl(tmp_path / "context-feedback.jsonl", rows)
    rep = fb.build_report(tmp_path)
    for o in fb.ALL_OUTCOMES:
        assert rep["outcomes"].get(o) == 1, f"outcome {o!r} not counted"
    # `failed` is surfaced in the rendered usefulness line.
    text = fb.render_text(tmp_path)
    assert "failed=" in text
    assert all(o in text for o in ("accepted", "failed", "manual_baseline"))


def test_decidable_ratio_passes(tmp_path: Path) -> None:
    """≥5 surfaced proposals per bucket with high accepting more than low → a real,
    decidable PASS ratio (the Goal-1 ≥2× bar)."""
    rows: list[dict] = []
    rows += [_swift_line("accepted", importance=0.8) for _ in range(5)]  # high: 5 accept
    rows += [_swift_line("dismissed", importance=0.8) for _ in range(1)]  # high: 1 decline
    rows += [_swift_line("accepted", importance=0.2) for _ in range(1)]  # low: 1 accept
    rows += [_swift_line("dismissed", importance=0.2) for _ in range(5)]  # low: 5 decline
    _write_jsonl(tmp_path / "context-feedback.jsonl", rows)

    corr = fb.accept_by_importance(fb.load_jsonl(tmp_path / "context-feedback.jsonl"))
    assert corr["high"]["n"] == 6 and corr["low"]["n"] == 6
    # high accept-rate 5/6 ≈ 0.833, low 1/6 ≈ 0.167 → ratio ≈ 5.0 ≥ 2.0
    assert corr["ratio"] is not None and corr["ratio"] >= fb.CORRELATION_TARGET
    assert corr["verdict"].startswith("PASS")
    assert fb.accrual_status(fb.load_jsonl(tmp_path / "context-feedback.jsonl"))["decidable"]


def test_sparse_is_undecidable_with_distance(tmp_path: Path) -> None:
    """Below the per-bucket floor → UNDECIDABLE (never a fake ratio), and the accrual
    line reports exactly how many more each bucket needs."""
    rows = [_swift_line("accepted", importance=0.8) for _ in range(2)] + [
        _swift_line("dismissed", importance=0.2) for _ in range(2)
    ]
    _write_jsonl(tmp_path / "context-feedback.jsonl", rows)
    events = fb.load_jsonl(tmp_path / "context-feedback.jsonl")

    assert fb.accept_by_importance(events)["verdict"] == "UNDECIDABLE"
    acc = fb.accrual_status(events)
    assert acc["decidable"] is False
    assert acc["need_high"] == fb.MIN_BUCKET_N - 2
    assert acc["need_low"] == fb.MIN_BUCKET_N - 2
    assert "还差" in fb.render_text(tmp_path)


def test_completed_failed_excluded_from_correlation(tmp_path: Path) -> None:
    """completed/failed/manual_baseline are NOT verdicts on a surfaced proposal, so
    they don't pollute the accept-by-importance correlation — only its usefulness gap."""
    rows = [
        _swift_line("completed", importance=0.9),
        _swift_line("failed", importance=0.9),
        _swift_line("manual_baseline", importance=0.0),
        _swift_line("accepted", importance=0.9),
    ]
    _write_jsonl(tmp_path / "context-feedback.jsonl", rows)
    events = fb.load_jsonl(tmp_path / "context-feedback.jsonl")
    corr = fb.accept_by_importance(events)
    # Only the single `accepted` is a verdict on a push.
    assert corr["high"]["n"] == 1 and corr["low"]["n"] == 0
    use = fb.usefulness(events)
    assert use["accepted"] == 1 and use["completed"] == 1 and use["failed"] == 1


def test_missing_file_is_empty(tmp_path: Path) -> None:
    """No telemetry yet → an empty, non-crashing report (the day-0 state)."""
    rep = fb.build_report(tmp_path)
    assert rep["n_feedback"] == 0
    assert rep["accept_by_importance"]["verdict"] == "UNDECIDABLE"
    assert "feedback events: 0" in fb.render_text(tmp_path)


# ── A1: calibration analyzer ────────────────────────────────────────────────


def test_confidence_calibration_monotone_is_calibrated(tmp_path: Path) -> None:
    """Two decidable confidence bins where accept-rate RISES with confidence → the
    recognizer's confidence tracks real acceptance → CALIBRATED."""
    rows: list[dict] = []
    # conf 0.7 bin: 2 accept / 3 decline = rate 0.40 (n=5, decidable)
    rows += [_swift_line("accepted", confidence=0.7) for _ in range(2)]
    rows += [_swift_line("dismissed", confidence=0.7) for _ in range(3)]
    # conf 0.9 bin: 4 accept / 1 decline = rate 0.80 (n=5, decidable)
    rows += [_swift_line("accepted", confidence=0.9) for _ in range(4)]
    rows += [_swift_line("ignored", confidence=0.9) for _ in range(1)]
    _write_jsonl(tmp_path / "context-feedback.jsonl", rows)

    cal = fb.confidence_calibration(fb.load_jsonl(tmp_path / "context-feedback.jsonl"))
    ranges = {r["range"]: r for r in cal["buckets"]}
    assert ranges["0.7-0.8"]["rate"] == 0.4 and ranges["0.7-0.8"]["decidable"]
    assert ranges["0.9-1.0"]["rate"] == 0.8 and ranges["0.9-1.0"]["decidable"]
    assert cal["verdict"].startswith("CALIBRATED")


def test_confidence_calibration_inverted_is_miscalibrated(tmp_path: Path) -> None:
    """Accept-rate FALLS as confidence rises across two decidable bins → the ≥0.7 gate
    is sorting on a number that doesn't track acceptance → MISCALIBRATED (the A2 cue)."""
    rows: list[dict] = []
    rows += [_swift_line("accepted", confidence=0.7) for _ in range(4)]  # 0.7 bin rate 0.80
    rows += [_swift_line("dismissed", confidence=0.7) for _ in range(1)]
    rows += [_swift_line("accepted", confidence=0.9) for _ in range(1)]  # 0.9 bin rate 0.20
    rows += [_swift_line("dismissed", confidence=0.9) for _ in range(4)]
    _write_jsonl(tmp_path / "context-feedback.jsonl", rows)

    cal = fb.confidence_calibration(fb.load_jsonl(tmp_path / "context-feedback.jsonl"))
    assert cal["verdict"].startswith("MISCALIBRATED")


def test_confidence_calibration_sparse_is_undecidable(tmp_path: Path) -> None:
    """Fewer than two decidable bins → UNDECIDABLE (never a fake calibration verdict),
    and the reason names the <0.7-needs-A3 caveat."""
    rows = [_swift_line("accepted", confidence=0.8) for _ in range(5)]  # one bin only
    _write_jsonl(tmp_path / "context-feedback.jsonl", rows)
    cal = fb.confidence_calibration(fb.load_jsonl(tmp_path / "context-feedback.jsonl"))
    assert cal["verdict"] == "UNDECIDABLE"
    assert "A3" in cal["reason"]


def test_per_kind_accept_decidability(tmp_path: Path) -> None:
    """Per-kind accept-rate is the A2 signal: a kind ≥MIN_BUCKET_N is decidable with a
    real rate; a kind below it is reported but NOT decidable (can't masquerade)."""
    rows: list[dict] = []
    rows += [_swift_line("accepted", kind="info_need") for _ in range(3)]
    rows += [_swift_line("dismissed", kind="info_need") for _ in range(2)]  # n=5 → decidable
    rows += [_swift_line("dismissed", kind="meeting_hint") for _ in range(2)]  # n=2 → not
    _write_jsonl(tmp_path / "context-feedback.jsonl", rows)

    pk = fb.per_kind_accept(fb.load_jsonl(tmp_path / "context-feedback.jsonl"))
    assert pk["kinds"]["info_need"] == {"n": 5, "accepted": 3, "rate": 0.6, "decidable": True}
    assert pk["kinds"]["meeting_hint"]["decidable"] is False
    assert pk["decidable"] is True  # at least one kind crossed the floor


def test_calibration_excludes_non_verdicts(tmp_path: Path) -> None:
    """completed/failed/manual_baseline are not verdicts on a surfaced proposal, so they
    enter NEITHER the confidence calibration NOR the per-kind accept tables."""
    rows = [
        _swift_line("completed", confidence=0.9, kind="reminder"),
        _swift_line("failed", confidence=0.9, kind="reminder"),
        _swift_line("manual_baseline", confidence=0.0, kind="reminder"),
    ]
    _write_jsonl(tmp_path / "context-feedback.jsonl", rows)
    events = fb.load_jsonl(tmp_path / "context-feedback.jsonl")
    assert fb.confidence_calibration(events)["buckets"] == []
    assert fb.per_kind_accept(events)["kinds"] == {}


def test_calibration_sections_render(tmp_path: Path) -> None:
    """Both A1 sections appear in the human report."""
    _write_jsonl(tmp_path / "context-feedback.jsonl", [_swift_line("accepted", confidence=0.8)])
    text = fb.render_text(tmp_path)
    assert "A1 置信度校准" in text
    assert "A1 每 kind accept 率" in text
