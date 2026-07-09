"""intent/facets.py 的确定性单测（零 LLM、零网络）。

覆盖 spec ``2026-06-26-faceted-fast-path-intent-schema-design.md`` 的装配规则与
迁移桥投影：
- ③ 时间性：when_text→scheduled / 空→open；hint 覆盖→conditional/recurring；
- ④ 来源：方向先验 + received+committed 低置信压回 proposed；
- ② 对象：object_hint（self 特判）> 对手方 > ambient；
- ⑤ 外向：Telos→外向 数据表；
- project_kind：commit×object 投影 meeting/reminder/calendar + produce→assignment（delegate 归一到 produce）+ 兜底。
"""

from __future__ import annotations

from persome.intent.facets import (
    OUTWARDNESS,
    TELOS_OUTWARDNESS,
    Facets,
    assemble,
    project_kind,
)

# ── ③ 时间性 ─────────────────────────────────────────────────────────────── #


def test_temporality_scheduled_when_when_text_present() -> None:
    f = assemble({"telos": "commit"}, direction="incoming", counterpart="A", when_text="明天3点")
    assert f.temporality == "scheduled"


def test_temporality_open_when_no_time() -> None:
    f = assemble({"telos": "acquire"}, direction="incoming", counterpart="A", when_text="")
    assert f.temporality == "open"


def test_recurrence_hint_overrides_to_recurring() -> None:
    f = assemble(
        {"telos": "produce", "recurrence_hint": "daily"},
        direction="outgoing",
        counterpart="",
        when_text="",
    )
    assert f.temporality == "recurring"
    assert f.recurrence == "daily"


def test_condition_hint_overrides_to_conditional() -> None:
    f = assemble(
        {"telos": "produce", "condition_hint": "发布完成"},
        direction="outgoing",
        counterpart="",
        when_text="",
    )
    assert f.temporality == "conditional"
    assert f.condition == "发布完成"


def test_condition_hint_wins_over_when_text() -> None:
    """hint 覆盖表面默认：condition 在场即使有 when_text 也判 conditional。"""
    f = assemble(
        {"telos": "commit", "condition_hint": "CI 变绿"},
        direction="incoming",
        counterpart="A",
        when_text="明天",
    )
    assert f.temporality == "conditional"


# ── ④ 来源（方向校验）────────────────────────────────────────────────────── #


def test_provenance_direction_prior_outgoing_is_committed() -> None:
    f = assemble({"telos": "commit"}, direction="outgoing", counterpart="A", when_text="8点")
    assert f.provenance == "committed"
    assert f.payload_provenance == "user_committed"


def test_provenance_direction_prior_incoming_is_proposed() -> None:
    f = assemble({"telos": "commit"}, direction="incoming", counterpart="A", when_text="8点")
    assert f.provenance == "proposed"
    assert f.payload_provenance == "counterpart_proposed"


def test_provenance_received_committed_low_conf_demoted() -> None:
    """LLM 说 committed 但方向 received 且置信不高 → 压回 proposed（防误动作）。"""
    f = assemble(
        {"telos": "commit", "provenance": "committed"},
        direction="incoming",
        counterpart="A",
        when_text="8点",
        confidence=0.6,
    )
    assert f.provenance == "proposed"


def test_provenance_received_committed_high_conf_kept() -> None:
    f = assemble(
        {"telos": "commit", "provenance": "committed"},
        direction="incoming",
        counterpart="A",
        when_text="8点",
        confidence=0.95,
    )
    assert f.provenance == "committed"


def test_provenance_sent_committed_kept_regardless() -> None:
    f = assemble(
        {"telos": "commit", "provenance": "committed"},
        direction="outgoing",
        counterpart="A",
        when_text="8点",
        confidence=0.5,
    )
    assert f.provenance == "committed"


# ── ② 对象 ───────────────────────────────────────────────────────────────── #


def test_object_defaults_to_counterpart() -> None:
    f = assemble({"telos": "commit"}, direction="incoming", counterpart="张三", when_text="3点")
    assert f.object == "person"
    assert f.object_entity == "张三"


def test_object_self_token_marks_self() -> None:
    f = assemble(
        {"telos": "commit", "object_hint": "self"},
        direction="outgoing",
        counterpart="张三",
        when_text="明天",
    )
    assert f.object == "self"
    assert f.object_entity is None


def test_object_hint_overrides_counterpart() -> None:
    """对象≠对手方：object_hint 命名了正文里提到的别人。"""
    f = assemble(
        {"telos": "commit", "object_hint": "李四"},
        direction="incoming",
        counterpart="张三",
        when_text="3点",
    )
    assert f.object == "person"
    assert f.object_entity == "李四"


def test_object_ambient_when_no_counterpart() -> None:
    f = assemble({"telos": "monitor"}, direction="unknown", counterpart="", when_text="")
    assert f.object == "ambient"


# ── ⑤ 外向 ───────────────────────────────────────────────────────────────── #


def test_outwardness_from_telos_table() -> None:
    assert (
        assemble(
            {"telos": "commit"}, direction="incoming", counterpart="A", when_text="3点"
        ).outwardness
        == "outward_reversible"
    )
    assert (
        assemble(
            {"telos": "transact"}, direction="outgoing", counterpart="A", when_text=""
        ).outwardness
        == "outward_irreversible"
    )
    assert (
        assemble(
            {"telos": "monitor"}, direction="unknown", counterpart="", when_text=""
        ).outwardness
        == "internal"
    )


def test_outwardness_table_total_over_telos() -> None:
    for telos, out in TELOS_OUTWARDNESS.items():
        assert out in OUTWARDNESS, f"{telos}→{out} 不在外向全域"


def test_unknown_telos_degrades_to_acquire() -> None:
    f = assemble({"telos": "frobnicate"}, direction="incoming", counterpart="A", when_text="")
    assert f.telos == "acquire"  # R6 降级，永不 KeyError


# ── project_kind 迁移桥 ───────────────────────────────────────────────────── #


def _f(telos: str, obj: str, temp: str = "scheduled") -> Facets:
    return Facets(
        telos=telos,
        object=obj,
        temporality=temp,
        provenance="proposed",
        outwardness=TELOS_OUTWARDNESS.get(telos, "internal"),
    )


def test_project_kind_commit_person_is_meeting() -> None:
    assert project_kind(_f("commit", "person")) == "meeting"


def test_project_kind_commit_self_is_reminder() -> None:
    assert project_kind(_f("commit", "self")) == "reminder"


def test_project_kind_commit_ambient_is_calendar() -> None:
    assert project_kind(_f("commit", "ambient")) == "calendar"


def test_project_kind_produce_is_assignment() -> None:
    assert project_kind(_f("produce", "self", temp="recurring")) == "assignment"
    assert project_kind(_f("produce", "person")) == "assignment"


def test_delegate_normalizes_to_produce_then_assignment() -> None:
    # §9 telos orthogonalization: `delegate` retired → normalized to `produce` at the entrance
    # (`_norm_telos`), so a model/legacy row that still emits it stays kind=assignment.
    f = assemble({"telos": "delegate"}, direction="outgoing", counterpart="小王", when_text="")
    assert f.telos == "produce"
    assert project_kind(f) == "assignment"


def test_project_kind_acquire_monitor_fallback_info_need() -> None:
    assert project_kind(_f("acquire", "ambient")) == "info_need"
    assert project_kind(_f("monitor", "ambient")) == "info_need"
