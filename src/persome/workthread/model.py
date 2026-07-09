"""WorkThread dataclasses — the closed-set state machine (spec §三).

Five statuses (closed set): ``active`` / ``background`` / ``done`` / ``stale``
/ ``superseded`` (absorbed by a merge — the fifth state F9 added).

``active`` uniqueness (at most one at any moment) is decided by the hysteresis
competition in :mod:`.executor`, never by the LLM. ``stale`` is harvested by
the daily tick (30 days without an attach) — inactivity is NEVER completion.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

# Closed status set (spec §三, five states incl. the merge-absorbed fifth).
STATUSES: tuple[str, ...] = ("active", "background", "done", "stale", "superseded")
# Statuses that count as "open" — visible to attach competition and recall.
OPEN_STATUSES: tuple[str, ...] = ("active", "background")
# Origin types (the "出生证明"). "Kevin 交办" is just origin_type=assignment.
ORIGIN_TYPES: tuple[str, ...] = ("assignment", "self_initiated", "meeting_action", "recurring")

# Hysteresis: a candidate must win at least this share of the latest
# aggregation window's assigned span-minutes to take ``active`` away from the
# incumbent (spec §三 — keeps 7.6-min micro-session noise from flipping
# ``active`` dozens of times a day).
ACTIVE_TAKEOVER_SHARE = 0.6

# Days without an attach before the daily tick harvests an open thread to
# ``stale`` (spec §三; aligned with the all-history dedup gate, not 14d).
STALE_AFTER_DAYS = 30


@dataclass
class Binding:
    """One aggregation window's evidence link: which spans fed this thread."""

    window_id: str  # aggregation window id (ISO of window end)
    session_ids: list[str] = field(default_factory=list)
    spans: list[list[str]] = field(default_factory=list)  # [["HH:MM","HH:MM"], ...]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> Binding:
        return cls(
            window_id=str(raw.get("window_id") or ""),
            session_ids=[str(s) for s in (raw.get("session_ids") or [])],
            # Skip malformed (non-length-2) spans instead of hard-unpacking — a
            # truncated / over-long / migrated dirty row must not crash the whole
            # table read via _row_to_thread (#588). Mirrors ThreadOp.from_dict.
            spans=[
                [str(p[0]), str(p[1])]
                for p in (raw.get("spans") or [])
                if isinstance(p, (list, tuple)) and len(p) == 2
            ],
        )


@dataclass
class WorkThread:
    """One undertaking — the join key along the identity axis (spec §三)."""

    id: str
    title: str
    goal: str = ""
    # --- 出生证明 -----------------------------------------------------------
    origin_type: str = "self_initiated"
    origin_actor: str = ""
    origin_evidence: list[dict] = field(default_factory=list)  # IntentEvidence shapes
    origin_at: str = ""
    origin_intent_id: int | None = None  # binds the S0 assignment intent
    # --- 状态机（闭集，五态） --------------------------------------------------
    status: str = "background"
    # --- 时间账（确定性累计；spans 契约见 executor） -----------------------------
    first_seen: str = ""
    last_active: str = ""
    total_active_minutes: int = 0
    # Set when any window had overlapping spans split evenly across threads —
    # the minutes figure is then a fair-share estimate, not an exact count.
    approximate: bool = False
    # --- 证据链与进展 ---------------------------------------------------------
    bindings: list[Binding] = field(default_factory=list)
    progress_notes: list[str] = field(default_factory=list)
    # --- 信任与纠错 -----------------------------------------------------------
    confidence: float = 0.5
    pinned: bool = False  # 人工确认线：不可被 merge 吸收 / 不可被 stale 收割
    user_corrected: int = 0

    def to_row_json(self) -> dict[str, str]:
        return {
            "origin_evidence": json.dumps(self.origin_evidence, ensure_ascii=False),
            "bindings": json.dumps([b.to_dict() for b in self.bindings], ensure_ascii=False),
            "progress_notes": json.dumps(self.progress_notes, ensure_ascii=False),
        }


@dataclass
class ThreadOp:
    """One operation from the tracker's closed set (spec §四 ThreadOp 契约)."""

    op: str  # open | attach | progress | merge | complete | none
    thread_id: str = ""
    title: str = ""
    goal: str = ""
    origin_type: str = ""
    origin_actor: str = ""
    origin_quote: str = ""
    spans: list[list[str]] = field(default_factory=list)
    note: str = ""
    evidence_quote: str = ""
    from_id: str = ""
    into_id: str = ""
    confidence: float = 0.5

    @classmethod
    def from_dict(cls, raw: dict) -> ThreadOp | None:
        """Coerce one LLM-emitted dict into a ThreadOp; None drops it silently.

        Unknown op names are dropped (closed set — the executor never grows a
        verb because the model invented one).
        """
        if not isinstance(raw, dict):
            return None
        op = str(raw.get("op") or "").strip()
        if op not in ("open", "attach", "progress", "merge", "complete", "none"):
            return None
        spans: list[list[str]] = []
        for pair in raw.get("spans") or []:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                spans.append([str(pair[0]).strip(), str(pair[1]).strip()])
        try:
            confidence = float(raw.get("confidence", 0.5) or 0.5)
        except (TypeError, ValueError):
            confidence = 0.5
        return cls(
            op=op,
            thread_id=str(raw.get("thread_id") or "").strip(),
            title=str(raw.get("title") or "").strip(),
            goal=str(raw.get("goal") or "").strip(),
            origin_type=str(raw.get("origin_type") or "").strip(),
            origin_actor=str(raw.get("origin_actor") or "").strip(),
            origin_quote=str(raw.get("origin_quote") or "").strip(),
            spans=spans,
            note=str(raw.get("note") or "").strip(),
            evidence_quote=str(raw.get("evidence_quote") or "").strip(),
            from_id=str(raw.get("from_id") or "").strip(),
            into_id=str(raw.get("into_id") or "").strip(),
            confidence=max(0.0, min(1.0, confidence)),
        )
