"""WorkThread — the "present continuous" layer (工作线).

The pipeline compresses along ONE axis — time (capture → timeline block →
micro-session → event-daily) — and every level answers "那段时间发生了什么".
"任务" is not a property of a time span: it is *identity* — this hour and
yesterday's two hours are the SAME undertaking. WorkThread is the join key
that folds scattered activity along that second, orthogonal axis.

Design: ``docs/superpowers/specs/2026-06-12-workthread-design.md`` (v2.1).
Constitution: evidence is an open set, operations are a closed set — the LLM
tracker only emits six ops (open/attach/progress/merge/complete/none); all
state transitions and ALL time accounting are executed by deterministic code
(:mod:`.executor`). Minutes are never reported by the model.
"""
