"""Tests for the unified intent mechanism (ontology + store + sink + recall)."""

from __future__ import annotations

import pytest

from persome.intent import recall, sink
from persome.intent import store as intent_store
from persome.intent.ontology import Intent, IntentEvidence
from persome.store import entries as entries_mod
from persome.store import fts

_DAY_START = "2026-05-31T00:00"
_DAY_END = "2026-06-01T00:00"


def _intent(
    *, kind: str = "meeting", when: str = "明天下午3点", people: list[str] | None = None
) -> Intent:
    return Intent(
        kind=kind,
        scope="timeline",
        confidence=0.9,
        rationale="user accepted the time",
        ts="2026-05-31T15:00",
        payload={
            "when_text": when,
            "with": people if people is not None else ["Alice"],
            "channel": "Lark",
        },
        evidence=[
            IntentEvidence(
                source="timeline_block", ref_id="blk-1", entry_index=0, quote="好啊明天下午3点聊"
            )
        ],
    )


# --- ontology -----------------------------------------------------------------


def test_ontology_roundtrip() -> None:
    it = _intent()
    again = Intent.from_dict(it.to_dict())
    assert again.kind == "meeting"
    assert again.scope == "timeline"
    assert again.payload["when_text"] == "明天下午3点"
    assert again.payload["with"] == ["Alice"]
    assert again.evidence[0].ref_id == "blk-1"
    assert again.evidence[0].entry_index == 0


def test_to_text_is_keyword_searchable() -> None:
    text = _intent().to_text()
    assert "[meeting]" in text
    assert "明天下午3点" in text
    assert "Alice" in text


def test_evidence_from_dict_tolerates_missing() -> None:
    ev = IntentEvidence.from_dict(None)
    assert ev.entry_index == -1 and ev.ref_id == ""


# --- store + sink (need PERSOME_ROOT via ac_root) -----------------------


def test_persist_and_recent(ac_root) -> None:  # noqa: ANN001
    with fts.cursor() as conn:
        row_id = sink.persist_intent(conn, _intent())
        assert row_id
        got = intent_store.recent_intents(conn, start=_DAY_START, end=_DAY_END, scope="timeline")
    assert len(got) == 1
    assert got[0].kind == "meeting"
    assert got[0].payload["when_text"] == "明天下午3点"


def test_persist_is_idempotent_by_dedup_key(ac_root) -> None:  # noqa: ANN001
    with fts.cursor() as conn:
        assert sink.persist_intent(conn, _intent()) is not None
        assert sink.persist_intent(conn, _intent()) is None  # duplicate → skipped
        got = intent_store.recent_intents(conn, start=_DAY_START, end=_DAY_END)
    assert len(got) == 1


def test_dedup_key_temporal_is_scope_agnostic() -> None:
    """A temporal intent folds across scopes: a session cut (or the timeline /
    trajectory shadow overlap) must not double-store the same meeting."""
    a = _intent()  # scope="timeline"
    b = Intent.from_dict({**a.to_dict(), "scope": "session-42"})
    assert intent_store.dedup_key(a) == intent_store.dedup_key(b)
    assert "timeline" not in intent_store.dedup_key(a)


def test_dedup_key_content_only_keeps_scope() -> None:
    """A content-only intent (no when/with) stays scope-scoped so the same hint
    in two different scenes coexists."""
    a = Intent(kind="info_need", scope="session-1", payload={"text": "查 manus 新闻"})
    b = Intent(kind="info_need", scope="session-2", payload={"text": "查 manus 新闻"})
    assert intent_store.dedup_key(a) != intent_store.dedup_key(b)
    assert "session-1" in intent_store.dedup_key(a)


def test_dedup_key_content_only_ignores_rationale_wording() -> None:
    """#529 regression: a content-only intent's dedup key must hash ONLY the
    normalized payload, NOT the LLM-authored ``rationale``. The same ``info_need``
    (identical payload) is re-phrased every ~60s block-flush re-recognition; if
    rationale fed the digest, each round minted a new key → new insert → HUD
    republish, directly violating the 弹错=复利损失 constitution."""
    a = Intent(
        kind="info_need",
        scope="session-1",
        rationale="用户想查 manus 最新进展",
        payload={"text": "查 manus 新闻"},
    )
    b = Intent(
        kind="info_need",
        scope="session-1",
        rationale="用户似乎在关注 manus 的动态",
        payload={"text": "查 manus 新闻"},
    )
    assert intent_store.dedup_key(a) == intent_store.dedup_key(b)
    # Different structured hints in the same scene still coexist.
    c = Intent(
        kind="info_need",
        scope="session-1",
        rationale="用户想查 manus 最新进展",
        payload={"text": "查 figma 新闻"},
    )
    assert intent_store.dedup_key(a) != intent_store.dedup_key(c)


def test_persist_content_only_folds_rationale_variants(ac_root) -> None:  # noqa: ANN001
    """End-to-end #529: persisting the same content-only ``info_need`` twice with
    different rationale wording stores ONE row, not two."""
    a = Intent(
        kind="info_need",
        scope="session-1",
        rationale="用户想查 manus 最新进展",
        payload={"text": "查 manus 新闻"},
    )
    b = Intent(
        kind="info_need",
        scope="session-1",
        rationale="用户似乎在关注 manus 的动态",
        payload={"text": "查 manus 新闻"},
    )
    with fts.cursor() as conn:
        intent_store.ensure_schema(conn)
        sink.persist_intent(conn, a)
        sink.persist_intent(conn, b)
        rows = conn.execute(
            "SELECT id FROM intents WHERE kind = 'info_need' AND scope = 'session-1'"
        ).fetchall()
    assert len(rows) == 1


def test_persist_folds_same_meeting_across_sessions(ac_root) -> None:  # noqa: ANN001
    """Same meeting recognized under two session scopes → one row."""
    with fts.cursor() as conn:
        first = _intent()  # scope="timeline"
        second = Intent.from_dict({**first.to_dict(), "scope": "session-99"})
        assert sink.persist_intent(conn, first) is not None
        assert sink.persist_intent(conn, second) is None  # folded
        got = intent_store.recent_intents(conn, start=_DAY_START, end=_DAY_END)
    assert len(got) == 1


def test_recent_intents_window_and_scope_filter(ac_root) -> None:  # noqa: ANN001
    with fts.cursor() as conn:
        sink.persist_intent(conn, _intent(kind="calendar", when="周二 10 点"))
        # out-of-window query returns nothing
        empty = intent_store.recent_intents(conn, start="2026-06-02T00:00", end="2026-06-03T00:00")
        # wrong scope returns nothing
        wrong_scope = intent_store.recent_intents(
            conn, start=_DAY_START, end=_DAY_END, scope="meeting-xyz"
        )
    assert empty == []
    assert wrong_scope == []


def test_persist_projects_into_entries_fts(ac_root) -> None:  # noqa: ANN001
    """The projection must land in the `entries` FTS so search_memory finds it."""
    with fts.cursor() as conn:
        sink.persist_intent(conn, _intent(kind="reminder", when="周五前", people=["Bob"]))
        proj = conn.execute(
            "SELECT path, content FROM entries WHERE path LIKE 'intent-%'"
        ).fetchall()
        # retrievable the same way search_memory queries (FTS MATCH)
        hit = conn.execute("SELECT content FROM entries WHERE entries MATCH 'reminder'").fetchall()
    assert proj, "intent projection should be appended into entries"
    assert hit, "projected intent should be retrievable via FTS MATCH"


# --- recall -------------------------------------------------------------------


def test_recall_assembles_background_from_fts(ac_root) -> None:  # noqa: ANN001
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-x.md", description="project x", tags=["x"])
        entries_mod.append_entry(
            conn, name="project-x.md", content="uses DeepSeek API for chat", tags=["x"]
        )
        bundle = recall.assemble_background(conn, scope="timeline", hints=["DeepSeek"])
    assert "DeepSeek" in bundle
    assert "project-x.md" in bundle


def test_recall_empty_hints_returns_blank(ac_root) -> None:  # noqa: ANN001
    with fts.cursor() as conn:
        assert recall.assemble_background(conn, scope="timeline", hints=[]) == ""


# --- when_text surface normalization (dedup only) ------------------------------

_FOLD_GROUPS = [
    # The motivating bug: one meeting, several LLM spellings → one key.
    ("周五下午3点", "星期五15:00", "礼拜五下午3点", "週五15点", "Friday 3pm", "fri 3:00pm"),
    ("下午3点", "3pm", "15:00", "15点", "下午 3 点"),
    ("上午9点", "早上9点", "9am", "09:00", "9:00"),
    ("下午12点", "12pm", "中午12点", "12:00"),  # 下午12点 = 12:00, not 24:00
    ("晚上8点", "8pm", "下午8点", "20:00", "晚上 ８点"),  # fullwidth digit folds too
    ("今晚8点", "今天晚上8点", "tonight 8pm"),
    ("明天早上9点", "明早9点", "明天上午9:00", "tomorrow 9am"),
    ("后天15:00", "后天下午3点"),
    ("周三10点", "星期三上午10点", "週三10:00"),
    ("下午3点半", "3:30pm", "15:30"),
    ("周日10点", "星期天10点", "礼拜天10:00"),
    # #618: 相对周序前缀的拼写变体折叠到同一个 token（下/下个/下一/下一个 = +7）。
    ("下周五3点", "下个周五3点", "下一周五3点", "下一个周五3点"),
]


@pytest.mark.parametrize("group", _FOLD_GROUPS)
def test_normalize_when_text_folds_equivalent_surface_forms(group) -> None:  # noqa: ANN001
    norms = {intent_store.normalize_when_text(t) for t in group}
    assert len(norms) == 1, f"{group} did not fold: {norms}"


_DISTINCT_PAIRS = [
    ("周五15:00", "周四15:00"),  # different weekday
    ("下午3点", "下午4点"),  # different hour
    ("15:00", "15:30"),  # different minutes
    ("明天9点", "后天9点"),  # different date word
    ("晚上8点", "早上8点"),  # different period → different clock time
    # Bare hour keeps its literal value — resolving "3点" to am/pm would be
    # semantic guessing, which normalize_when_text deliberately refuses.
    ("周五3点", "周五下午3点"),
    ("下周五3点", "周五3点"),  # next-week marker is preserved, not folded away
    ("下下周五3点", "下周五3点"),  # #618: 下下周 (+14) ≠ 下周 (+7)
]


@pytest.mark.parametrize(("a", "b"), _DISTINCT_PAIRS)
def test_normalize_when_text_keeps_distinct_times_distinct(a, b) -> None:  # noqa: ANN001
    assert intent_store.normalize_when_text(a) != intent_store.normalize_when_text(b)


def test_normalize_when_text_is_deterministic_and_total() -> None:
    # Unparseable text passes through (lowercased/stripped) instead of raising.
    assert intent_store.normalize_when_text("3小时后") == "3小时后"
    assert intent_store.normalize_when_text("") == ""
    assert intent_store.normalize_when_text("周五3点，") == intent_store.normalize_when_text(
        "周五3点"
    )


def test_dedup_key_folds_when_text_surface_variants() -> None:
    a = _intent(when="周五下午3点")
    b = _intent(when="星期五15:00")
    assert intent_store.dedup_key(a) == intent_store.dedup_key(b)
    # …but different people still split the key.
    c = _intent(when="星期五15:00", people=["Bob"])
    assert intent_store.dedup_key(a) != intent_store.dedup_key(c)


def test_persist_folds_when_text_surface_variants(ac_root) -> None:  # noqa: ANN001
    with fts.cursor() as conn:
        assert sink.persist_intent(conn, _intent(when="周五下午3点")) is not None
        assert sink.persist_intent(conn, _intent(when="星期五15:00")) is None  # folded
        got = intent_store.recent_intents(conn, start=_DAY_START, end=_DAY_END)
    assert len(got) == 1
    # Storage keeps the ORIGINAL text — normalization is dedup-key-only.
    assert got[0].payload["when_text"] == "周五下午3点"


def test_persist_folds_onto_legacy_raw_key_row(ac_root) -> None:  # noqa: ANN001
    """Migration shim: rows persisted before normalization carry the raw-text
    dedup key; a re-recognition with the same raw text must fold onto them."""
    it = _intent(when="周五下午3点")
    legacy = intent_store.legacy_dedup_key(it)
    assert legacy is not None and legacy != intent_store.dedup_key(it)
    with fts.cursor() as conn:
        row_id = intent_store.insert_intent(conn, it)
        # Rewrite the stored key to the pre-normalization (raw when_text) form.
        conn.execute("UPDATE intents SET dedup_key = ? WHERE id = ?", (legacy, row_id))
        conn.commit()
        assert sink.persist_intent(conn, it) is None  # folds via legacy key
        assert intent_store.id_for_intent(conn, it) == row_id  # HUD write-back ok


def test_legacy_dedup_key_none_when_already_normalized() -> None:
    it = _intent(when="15:00")  # canonical form → normalization is a no-op
    assert intent_store.legacy_dedup_key(it) is None


# --- SSE publish for 识别即推 (#intent-auto-enqueue) --------------------------


def _publish_spy(monkeypatch):  # noqa: ANN001, ANN202
    """Record every ``events.publish`` call the sink makes during the test."""
    calls: list[tuple[str, str, dict]] = []

    def _spy(stage: str, event_type: str, payload: dict) -> None:
        calls.append((stage, event_type, payload))

    # Patch the module object the sink actually calls (``events_mod``), so the
    # spy sees the publish regardless of import aliasing.
    monkeypatch.setattr(sink.events_mod, "publish", _spy)
    return calls


def _persisted_calls(calls):
    return [(s, t, p) for s, t, p in calls if s == "intent" and t == "persisted"]


def test_persist_open_intent_publishes_event(ac_root, monkeypatch) -> None:  # noqa: ANN001
    """A brand-new OPEN intent is published the instant it lands so the app can
    auto-enqueue without the reconcile poll."""
    calls = _publish_spy(monkeypatch)
    with fts.cursor() as conn:
        row_id = sink.persist_intent(conn, _intent())
    assert row_id is not None
    persisted = _persisted_calls(calls)
    assert len(persisted) == 1
    payload = persisted[0][2]
    assert payload["id"] == row_id  # committed row id, not the in-memory None
    assert payload["kind"] == "meeting"
    assert payload["status"] == "open"


def test_persist_armed_intent_does_not_publish(ac_root, monkeypatch) -> None:  # noqa: ANN001
    """An armed (fire_on) insert stays dormant — the activator's ``event_fired``
    covers its surfacing moment, so we never push an armed row (L7 时机门)."""
    calls = _publish_spy(monkeypatch)
    armed = Intent(
        kind="reminder",
        scope="session-armed",
        rationale="下次打开 Figma 时提醒改图标",
        payload={"text": "改图标"},
        fire_on="app_opened",
        fire_config={"app": "Figma"},
    )
    with fts.cursor() as conn:
        sink.persist_intent(conn, armed)
    assert _persisted_calls(calls) == []


def test_persist_skipped_duplicate_does_not_publish(ac_root, monkeypatch) -> None:  # noqa: ANN001
    """A dedup-skipped re-recognition publishes nothing — only a brand-new row
    surfaces, keeping the push stream noise-free (dedup denoises, never delays)."""
    calls = _publish_spy(monkeypatch)
    with fts.cursor() as conn:
        first = sink.persist_intent(conn, _intent())
        second = sink.persist_intent(conn, _intent())  # same dedup key → skipped
    assert first is not None
    assert second is None
    assert len(_persisted_calls(calls)) == 1  # only the first insert
