"""Intent precision · lifecycle audit oracle (`persome intent-audit`).

Drives `intent.audit` against a SYNTHETIC DB with a known shape and asserts each
aggregate exactly — so the oracle's numbers are trustworthy (a wrong oracle would
mislead every downstream tuning decision). Deterministic, zero-LLM, read-only.
"""

from __future__ import annotations

import json
import sqlite3

from persome.intent import audit

_NOW = "2026-06-30T12:00:00+08:00"


def _make_db(tmp_path, rows: list[dict]) -> str:
    """Build a minimal intents table with just the audited columns."""
    db = tmp_path / "index.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE intents (id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT, ts TEXT, "
        "valid_until TEXT, resolved_at TEXT, dismissed_at TEXT, kind TEXT, confidence REAL, payload TEXT)"
    )
    for r in rows:
        payload = {"text": r.get("text", "")}
        if "importance" in r:
            payload["importance"] = r["importance"]
        if "urgency" in r:
            payload["urgency"] = r["urgency"]
        conn.execute(
            "INSERT INTO intents (status, ts, valid_until, resolved_at, dismissed_at, kind, "
            "confidence, payload) VALUES (?,?,?,?,?,?,?,?)",
            (
                r["status"],
                r.get("ts", "2026-06-30T11:00:00+08:00"),
                r.get("valid_until"),
                r.get("resolved_at"),
                r.get("dismissed_at"),
                r.get("kind", "reminder"),
                r.get("confidence", 0.9),
                json.dumps(payload),
            ),
        )
    conn.commit()
    conn.close()
    return str(db)


def test_empty_db_is_failopen(tmp_path):
    assert audit.build_report(str(tmp_path / "nope.db"), now_iso=_NOW)["total"] == 0
    assert "no intents" in audit.render_text(str(tmp_path / "nope.db"), now_iso=_NOW)


def test_status_distribution_and_kind_crosstab(tmp_path):
    db = _make_db(
        tmp_path,
        [
            {"status": "open", "kind": "reminder", "text": "a"},
            {"status": "open", "kind": "meeting", "text": "b"},
            {"status": "consumed", "kind": "reminder", "text": "c"},
            {"status": "expired", "kind": "meeting", "text": "d"},
        ],
    )
    rep = audit.build_report(db, now_iso=_NOW)
    assert rep["total"] == 4
    assert rep["status_dist"] == {"consumed": 1, "expired": 1, "open": 2}
    assert rep["kind_status"]["reminder"] == {"consumed": 1, "open": 1}
    assert rep["kind_status"]["meeting"] == {"expired": 1, "open": 1}


def test_stale_open_age_and_ungrounded(tmp_path):
    db = _make_db(
        tmp_path,
        [
            {
                "status": "open",
                "ts": "2026-06-30T11:00:00+08:00",
                "text": "fresh",
            },  # <1d, ungrounded
            {
                "status": "open",
                "ts": "2026-06-27T11:00:00+08:00",
                "text": "midold",  # 1-7d
                "valid_until": "2026-07-10T00:00:00+08:00",
            },  # grounded
            {
                "status": "open",
                "ts": "2026-05-01T11:00:00+08:00",
                "text": "ancient",
            },  # >30d, ungrounded
        ],
    )
    rep = audit.build_report(db, now_iso=_NOW)["open"]
    assert rep["total"] == 3
    assert rep["by_age"] == {"<1d": 1, "1-7d": 1, "7-30d": 0, ">30d": 1}
    assert rep["ungrounded"] == 2  # the fresh + ancient have valid_until NULL
    assert rep["ungrounded_rate"] == round(2 / 3, 3)


def test_duplicate_clusters_among_open(tmp_path):
    db = _make_db(
        tmp_path,
        [
            {"status": "open", "text": "拉李敏进群"},
            {"status": "open", "text": "拉李敏进群"},
            {"status": "open", "text": "拉李敏进群"},  # cluster of 3
            {"status": "open", "text": "写周报"},
            {"status": "open", "text": "写周报"},  # cluster of 2
            {"status": "open", "text": "独一无二"},  # singleton, not a cluster
            {"status": "consumed", "text": "拉李敏进群"},  # consumed doesn't count toward open dup
        ],
    )
    dup = audit.build_report(db, now_iso=_NOW)["duplicates"]
    assert dup["clusters"] == 2
    assert dup["max_cluster"] == 3
    assert dup["cluster_sizes"] == {"2": 1, "3": 1}


def test_re_mention_handled_then_open(tmp_path):
    db = _make_db(
        tmp_path,
        [
            # handled earlier, re-opened later → counts
            {"status": "dismissed", "ts": "2026-06-29T10:00:00+08:00", "text": "交水电费"},
            {"status": "open", "ts": "2026-06-30T10:00:00+08:00", "text": "交水电费"},
            # open BEFORE it was handled → not a re-mention
            {"status": "open", "ts": "2026-06-28T10:00:00+08:00", "text": "买菜"},
            {"status": "consumed", "ts": "2026-06-29T10:00:00+08:00", "text": "买菜"},
        ],
    )
    assert audit.build_report(db, now_iso=_NOW)["re_mention"] == 1


def test_accept_dismiss_splits_user_vs_harvest(tmp_path):
    db = _make_db(
        tmp_path,
        [
            {"status": "consumed", "text": "a"},
            {"status": "consumed", "text": "b"},
            {
                "status": "dismissed",
                "dismissed_at": "2026-06-29T10:00:00+08:00",
                "text": "c",
            },  # user
            {"status": "dismissed", "text": "d"},  # harvest (no dismissed_at)
            {"status": "dismissed", "text": "e"},  # harvest
        ],
    )
    ad = audit.build_report(db, now_iso=_NOW)["accept_dismiss"]
    assert ad["consumed"] == 2
    assert ad["dismissed_user"] == 1
    assert ad["dismissed_harvest"] == 2
    assert ad["accept_rate"] == round(2 / 3, 3)  # 2 consumed / (2 + 1 user-dismiss)


def test_fold_gap_grounded_plus_ungrounded(tmp_path):
    db = _make_db(
        tmp_path,
        [
            {"status": "open", "text": "同步进度", "resolved_at": "2026-07-01T09:00:00+08:00"},
            {"status": "open", "text": "同步进度"},  # ungrounded twin → fold gap
            {
                "status": "open",
                "text": "只有一种",
                "resolved_at": "2026-07-01T09:00:00+08:00",
            },  # only grounded, no gap
        ],
    )
    assert audit.build_report(db, now_iso=_NOW)["fold_gap"] == 1


def test_re_mention_counts_same_tick_reopen(tmp_path):
    """A handle + reopen landing in the SAME tick (equal ts) still counts — that
    simultaneous reopen IS the over-fire signal (the `>=` boundary). (The normal
    open-then-handled lifecycle is covered as a non-re-mention in 买菜 above.)"""
    db = _make_db(
        tmp_path,
        [
            {"status": "dismissed", "ts": "2026-06-30T10:00:00+08:00", "text": "同一刻"},
            {"status": "open", "ts": "2026-06-30T10:00:00+08:00", "text": "同一刻"},  # same ts
        ],
    )
    assert audit.build_report(db, now_iso=_NOW)["re_mention"] == 1


def test_accept_rate_undecidable_with_no_user_verdict(tmp_path):
    """No consumed + only harvest dismissals → accept_rate is None (never faked)."""
    db = _make_db(
        tmp_path,
        [
            {"status": "dismissed", "text": "h1"},  # harvest (no dismissed_at)
            {"status": "expired", "text": "e1"},
        ],
    )
    ad = audit.build_report(db, now_iso=_NOW)["accept_dismiss"]
    assert ad["consumed"] == 0 and ad["dismissed_user"] == 0 and ad["dismissed_harvest"] == 1
    assert ad["accept_rate"] is None


def test_surfacing_softnag_counts_surfaced_then_reaped(tmp_path):
    """Soft-nag = an intent that DID surface (confidence ≥ 0.7 bar) yet got
    neither consumed nor explicitly dismissed — reaped as `expired` or as a
    harvest `dismissed` (NULL dismissed_at). Below-bar rows never surfaced, so
    they are excluded entirely; an explicit user-dismissal is a hard reject, not
    a soft-nag. true_surface_accept_rate folds soft-nags in as implicit rejects."""
    db = _make_db(
        tmp_path,
        [
            # surfaced + accepted
            {"status": "consumed", "kind": "meeting", "confidence": 0.95, "text": "a"},
            # surfaced + explicit user reject (NOT a soft-nag)
            {
                "status": "dismissed",
                "dismissed_at": "2026-06-29T10:00:00+08:00",
                "kind": "meeting",
                "confidence": 0.9,
                "text": "b",
            },
            # surfaced + ignored to expiry → soft-nag
            {"status": "expired", "kind": "info_need", "confidence": 0.88, "text": "c"},
            {"status": "expired", "kind": "info_need", "confidence": 0.7, "text": "d"},  # at bar
            {"status": "expired", "kind": "meeting", "confidence": 0.92, "text": "e"},
            # surfaced + harvest-dismissed (NULL dismissed_at) → soft-nag
            {"status": "dismissed", "kind": "reminder", "confidence": 0.8, "text": "f"},
            # BELOW bar → never surfaced → excluded from every soft-nag tally
            {"status": "expired", "kind": "info_need", "confidence": 0.4, "text": "g"},
            {"status": "consumed", "kind": "meeting", "confidence": 0.5, "text": "h"},
        ],
    )
    sn = audit.build_report(db, now_iso=_NOW)["surfacing_softnag"]
    assert sn["surface_bar"] == 0.7
    assert sn["surfaced_consumed"] == 1  # only the 0.95 consumed (0.5 consumed is below bar)
    assert sn["surfaced_dismissed_user"] == 1  # the 0.9 explicit dismiss
    assert sn["softnag"] == 4  # 3 surfaced-expired (0.88/0.7/0.92) + 1 surfaced-harvest (0.8)
    assert sn["softnag_by_kind"] == {"info_need": 2, "meeting": 1, "reminder": 1}
    # 1 / (1 consumed + 1 user-dismiss + 4 soft-nag) = 1/6
    assert sn["true_surface_accept_rate"] == round(1 / 6, 3)


def test_surfacing_softnag_undecidable_when_nothing_surfaces(tmp_path):
    """All rows below the surface bar → nothing surfaced → rate is None, never faked."""
    db = _make_db(
        tmp_path,
        [
            {"status": "consumed", "confidence": 0.3, "text": "a"},
            {"status": "expired", "confidence": 0.6, "text": "b"},
        ],
    )
    sn = audit.build_report(db, now_iso=_NOW)["surfacing_softnag"]
    assert sn["softnag"] == 0
    assert sn["true_surface_accept_rate"] is None


def test_empty_body_rows_do_not_collapse(tmp_path):
    """Rows with no body must NOT all collapse into one ""-keyed cluster/gap."""
    db = _make_db(
        tmp_path,
        [
            {"status": "open", "text": ""},
            {"status": "open", "text": ""},
            {"status": "open", "text": "", "resolved_at": "2026-07-01T09:00:00+08:00"},
            {"status": "dismissed", "text": ""},
            {"status": "open", "text": ""},
        ],
    )
    rep = audit.build_report(db, now_iso=_NOW)
    assert rep["duplicates"]["clusters"] == 0  # empty bodies are excluded
    assert rep["fold_gap"] == 0
    assert rep["re_mention"] == 0


def _verdict(rows, kind, tmp_path):
    return audit.build_report(_make_db(tmp_path, rows), now_iso=_NOW)["kind_separability"][kind]


def test_kind_separability_separable_by_confidence(tmp_path):
    """Accepted all high-confidence, rejected all low → a clean threshold exists."""
    rows = [
        {"status": "consumed", "kind": "meeting", "confidence": 0.95, "text": f"a{i}"}
        for i in range(4)
    ]
    rows += [
        {
            "status": "dismissed",
            "dismissed_at": "2026-06-29T10:00:00+08:00",
            "kind": "meeting",
            "confidence": 0.7,
            "text": f"d{i}",
        }
        for i in range(4)
    ]
    s = _verdict(rows, "meeting", tmp_path)
    assert s["verdict"] == "SEPARABLE"
    assert s["score"] == "confidence"
    assert s["kept_accept"] == 4 and s["dropped_reject"] == 4  # clean split
    assert s["j"] == 1.0


def test_kind_separability_not_separable_when_scores_overlap(tmp_path):
    """Accepted and rejected fully overlap on every score → NOT separable
    (the meeting reality: blindly raising a threshold would kill accepted too)."""
    rows = []
    for i in range(4):
        rows.append(
            {
                "status": "consumed",
                "kind": "meeting",
                "confidence": 0.8,
                "importance": 0.6,
                "urgency": 0.6,
                "text": f"a{i}",
            }
        )
        rows.append(
            {
                "status": "dismissed",
                "dismissed_at": "2026-06-29T10:00:00+08:00",
                "kind": "meeting",
                "confidence": 0.8,
                "importance": 0.6,
                "urgency": 0.6,
                "text": f"d{i}",
            }
        )
    s = _verdict(rows, "meeting", tmp_path)
    assert s["verdict"] == "NOT_SEPARABLE"
    assert s["j"] < 0.5


def test_kind_separability_insufficient_samples(tmp_path):
    """Too few verdicts per class → INSUFFICIENT, never a faked verdict."""
    rows = [
        {"status": "consumed", "kind": "info_need", "text": "a"},
        {
            "status": "dismissed",
            "dismissed_at": "2026-06-29T10:00:00+08:00",
            "kind": "info_need",
            "text": "d",
        },
    ]
    s = _verdict(rows, "info_need", tmp_path)
    assert s["verdict"] == "INSUFFICIENT"
    assert s["n_accept"] == 1 and s["n_reject"] == 1


def test_kind_separability_ignores_harvest_dismissals(tmp_path):
    """Only USER rejections (dismissed_at set) count — harvest dismissals are not
    a user verdict and must not enter the separability sample."""
    rows = [
        {"status": "consumed", "kind": "meeting", "confidence": 0.95, "text": f"a{i}"}
        for i in range(3)
    ]
    rows += [
        {"status": "dismissed", "kind": "meeting", "confidence": 0.7, "text": f"h{i}"}
        for i in range(5)
    ]  # harvest, no dismissed_at
    s = _verdict(rows, "meeting", tmp_path)
    assert s["verdict"] == "INSUFFICIENT"  # 3 accept but 0 user-reject
    assert s["n_reject"] == 0


def test_kind_separability_skips_iu_when_missing(tmp_path):
    """The missing-iu artifact: accepts carry importance×urgency, rejects DON'T.
    A naive iu=0-for-missing would fabricate a clean split; iu must be excluded so
    only confidence (here overlapping → J=0) decides → NOT_SEPARABLE."""
    rows = [
        {
            "status": "consumed",
            "kind": "meeting",
            "confidence": 0.8,
            "importance": 0.7,
            "urgency": 0.7,
            "text": f"a{i}",
        }
        for i in range(4)
    ]
    rows += [
        {
            "status": "dismissed",
            "dismissed_at": "2026-06-29T10:00:00+08:00",
            "kind": "meeting",
            "confidence": 0.8,
            "text": f"d{i}",
        }  # NO importance/urgency → iu None
        for i in range(4)
    ]
    s = _verdict(rows, "meeting", tmp_path)
    assert s["verdict"] == "NOT_SEPARABLE"  # iu artifact excluded; confidence overlaps
    assert s["score"] == "confidence"


def test_kind_separability_iu_can_win(tmp_path):
    """When iu genuinely separates (both classes carry it) and confidence doesn't,
    the iu axis wins the max-J selection."""
    rows = [
        {
            "status": "consumed",
            "kind": "meeting",
            "confidence": 0.8,
            "importance": 0.9,
            "urgency": 0.9,
            "text": f"a{i}",
        }
        for i in range(4)
    ]
    rows += [
        {
            "status": "dismissed",
            "dismissed_at": "2026-06-29T10:00:00+08:00",
            "kind": "meeting",
            "confidence": 0.8,
            "importance": 0.2,
            "urgency": 0.2,
            "text": f"d{i}",
        }
        for i in range(4)
    ]
    s = _verdict(rows, "meeting", tmp_path)
    assert s["verdict"] == "SEPARABLE" and s["score"] == "iu" and s["j"] == 1.0


def test_kind_separability_iu_winner_denominator_matches_iu_subset(tmp_path):
    """Issue #368: when the iu axis wins but only PART of the verdicts carry
    importance×urgency, kept_accept/dropped_reject are counted over the iu SUBSET
    (K), so their denominator must be that subset — NOT the full confidence
    verdict count (N). Here confidence overlaps (can't separate) while iu splits
    cleanly on the K=4 rows that carry it, so iu wins the max-J selection. The 8
    rows in test_kind_separability_iu_can_win all carry iu (subset == full), so
    that test never exercises the K < N denominator mismatch — this one does."""
    rows = []
    # 6 accepted, all confidence 0.8 (overlaps rejects → confidence can't split);
    # only K=4 carry a clean high importance×urgency (iu subset K=4 < N=6).
    rows += [
        {
            "status": "consumed",
            "kind": "meeting",
            "confidence": 0.8,
            "importance": 0.9,
            "urgency": 0.9,
            "text": f"a{i}",
        }
        for i in range(4)
    ]
    rows += [
        {"status": "consumed", "kind": "meeting", "confidence": 0.8, "text": f"an{i}"}
        for i in range(2)  # NO importance/urgency → excluded from the iu axis
    ]
    # 6 rejected, all confidence 0.8; only K=4 carry a clean low importance×urgency.
    rows += [
        {
            "status": "dismissed",
            "dismissed_at": "2026-06-29T10:00:00+08:00",
            "kind": "meeting",
            "confidence": 0.8,
            "importance": 0.2,
            "urgency": 0.2,
            "text": f"d{i}",
        }
        for i in range(4)
    ]
    rows += [
        {
            "status": "dismissed",
            "dismissed_at": "2026-06-29T10:00:00+08:00",
            "kind": "meeting",
            "confidence": 0.8,
            "text": f"dn{i}",
        }
        for i in range(2)  # NO importance/urgency → excluded from the iu axis
    ]
    s = _verdict(rows, "meeting", tmp_path)
    assert s["verdict"] == "SEPARABLE" and s["score"] == "iu"
    # numerators counted over the iu subset (K=4), never the full N=6 …
    assert s["kept_accept"] == 4 and s["dropped_reject"] == 4
    # … so the denominators MUST be that same subset (K=4). Pre-fix they were the
    # confidence full counts (N=6) — the mismatch this test pins.
    assert s["n_accept"] == 4 and s["n_reject"] == 4
    # the coherence invariant the mismatch violated in the rendered "kept X/Y" line:
    assert s["kept_accept"] <= s["n_accept"] and s["dropped_reject"] <= s["n_reject"]


def test_kind_separability_j_exactly_half_is_separable(tmp_path):
    """J == _SEP_J_BAR (0.5) is inclusive → SEPARABLE (pins the `>=` boundary)."""
    rows = [
        {"status": "consumed", "kind": "reminder", "confidence": 0.9, "text": f"a{i}"}
        for i in range(4)
    ]
    # at T=0.9: keep 4/4 accepted, drop 2/4 rejected → J = 1.0 + 0.5 − 1 = 0.5
    rej_conf = [0.8, 0.8, 0.9, 0.9]
    rows += [
        {
            "status": "dismissed",
            "dismissed_at": "2026-06-29T10:00:00+08:00",
            "kind": "reminder",
            "confidence": c,
            "text": f"d{i}",
        }
        for i, c in enumerate(rej_conf)
    ]
    s = _verdict(rows, "reminder", tmp_path)
    assert s["j"] == 0.5 and s["verdict"] == "SEPARABLE"


def test_kind_separability_flags_low_confidence_small_n(tmp_path):
    """A SEPARABLE verdict on a tiny accepted sample (n<8) carries low_confidence."""
    rows = [
        {"status": "consumed", "kind": "meeting", "confidence": 0.95, "text": f"a{i}"}
        for i in range(4)
    ]
    rows += [
        {
            "status": "dismissed",
            "dismissed_at": "2026-06-29T10:00:00+08:00",
            "kind": "meeting",
            "confidence": 0.7,
            "text": f"d{i}",
        }
        for i in range(4)
    ]
    s = _verdict(rows, "meeting", tmp_path)
    assert s["verdict"] == "SEPARABLE" and s["low_confidence"] is True


def test_render_text_is_pii_free_and_has_sections(tmp_path):
    db = _make_db(
        tmp_path,
        [{"status": "open", "text": "SENSITIVE_SECRET_BODY"}, {"status": "consumed", "text": "x"}],
    )
    text = audit.render_text(db, now_iso=_NOW)
    assert "SENSITIVE_SECRET_BODY" not in text  # never leaks body text
    assert "audit" in text and "ACCEPT vs DISMISS" in text and "OPEN" in text
