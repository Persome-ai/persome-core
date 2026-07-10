"""Canonical intent ontology — the single representation every recognizer emits.

Before this module there were three incompatible intent representations: the
timeline ``helpful_intent_tags`` dicts (``{meeting,calendar,reminder}``), the
``pending_actions`` rows, and the meeting analyzer's free-form push strings.
``Intent`` unifies them so any online scenario pack produces, and any consumer
reads, the same shape.

Design notes:
- ``kind`` is an OPEN string, not a closed enum: scenario packs may introduce
  new kinds (e.g. ``info_need`` for meeting "查一下 manus 新闻") without a schema
  migration. ``SEED_KINDS`` documents the ones in use today.
- ``scope`` identifies the scene the intent belongs to (``"timeline"`` for the
  always-on passive recognizer, a meeting id for a meeting pack, etc.) so the
  online runtime can group/replay intents per scene.
- ``evidence`` keeps verbatim provenance (source block + entry index + quote)
  so a consumer can always trace an intent back to raw signal.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

# Kinds in use today. NOT an enforced enum — packs may add more; this is the
# documented seed set so consumers know the baseline.
#
# ``assignment``: "X 让我做 Y / 你来负责 Z". Payload: ``task_text`` (what),
# ``assigned_by`` (who), ``channel``, optional ``deadline_text``. It lands in
# memory and surfaces through recall's scene layer.
#
# ``backlog`` (重要但不紧急 / important-but-not-urgent, proactive-anti-
# procrastination loop, plan 2026-06-17 §3 metric#1): a clearly-valuable,
# DEADLINE-FREE task the user keeps deferring (refactor X, write docs, read a
# paper, draft OKRs, organize notes, follow up on a health check). Recognized on
# the SLOW (session-trajectory) path ONLY — anchorless, so it never goes through
# the anchor-gated fast path, and it is NEVER emitted as meeting/calendar/
# reminder (anchorless ⇒ not schedulable). Payload: ``text`` (the task). Emitted
# at LOW confidence. Like ``assignment`` it is NOT in
# ``writer.active._PROPOSABLE_KINDS`` — it is a low-stakes signal the sentinel
# may surface, never an auto-popup, so a wrong recognition costs nothing here.
SEED_KINDS: tuple[str, ...] = ("meeting", "calendar", "reminder", "assignment", "backlog")


@dataclass
class IntentEvidence:
    """Verbatim provenance for one intent — where it came from in raw signal.

    ``source`` names what kind of id ``ref_id`` is, so consumers dispatch on it
    instead of guessing the ref format (#550 溯源统一). Canonical values:

    - ``"capture"``           — ref_id is a capture buffer file stem (fast K1 path)
    - ``"timeline_block"``    — ref_id is a ``timeline_blocks.id`` (slow trajectory path)
    - ``"meeting_transcript"``— ref_id is a meeting scope/transcript id (meeting pack)

    (Legacy rows persisted before #550 carry ``"session_trajectory"`` from both
    recognizer paths; consumers that load blocks still accept it.)
    """

    source: str = ""  # "capture" | "timeline_block" | "meeting_transcript"
    ref_id: str = ""  # capture stem / timeline block id / transcript id
    entry_index: int = -1  # index into the block's entries (-1 = n/a)
    quote: str = ""  # short verbatim quote backing the intent

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict | None) -> IntentEvidence:
        raw = raw or {}
        return cls(
            source=str(raw.get("source") or ""),
            ref_id=str(raw.get("ref_id") or ""),
            entry_index=int(raw.get("entry_index", -1)),
            quote=str(raw.get("quote") or ""),
        )


@dataclass
class Intent:
    """One recognized intent, scenario-agnostic.

    ``payload`` carries kind-specific structured fields (e.g. ``when_text``,
    ``with``, ``channel`` for calendar/meeting intents) so the ontology stays
    stable while packs extend the detail.
    """

    kind: str
    scope: str  # scene id: "timeline" | <meeting-id> | <chat-session-id> | ...
    confidence: float = 0.0
    rationale: str = ""
    status: str = "open"  # open | armed | consumed | dismissed | expired
    ts: str = ""  # ISO8601 (UTC-ish minute) of recognition
    payload: dict = field(default_factory=dict)
    evidence: list[IntentEvidence] = field(default_factory=list)
    id: int | None = None  # row id once persisted; not part of dedup/content
    # --- event-based prospective intent (Hy-Memory L7) -----------------------
    # A "dormant" intent waits for a future event before it surfaces, instead of
    # going straight to status=open. When ``fire_on`` is set, the sink stores it
    # as ``status="armed"`` (kept out of the open stream the active layer reads);
    # an activator flips it ``armed→open`` + stamps ``fired_at`` when the event
    # fires, after which the normal open→active→proposal chain takes over.
    #   fire_on     : event key. MVP: "app_opened". "" == an immediate intent.
    #   fire_config : trigger params, e.g. {"bundle_id": "com.figma...", "app": "Figma"}.
    #   fired_at    : ISO8601 when the trigger fired (None until then).
    fire_on: str = ""
    fire_config: dict = field(default_factory=dict)
    fired_at: str | None = None
    # --- schema provenance (R4 feedback loop) --------------------------------
    # The ``schema-*.md`` filenames whose inferences were injected into the
    # recognition context that produced this intent ("当时在场" — coarse
    # co-presence attribution, not a causal claim). A later HUD accept/dismiss
    # flows back onto these schemas' confidence via
    # ``intent.schema_feedback.apply_intent_feedback``. Kept OUT of ``payload``
    # on purpose: content-only intents hash their payload into ``dedup_key``,
    # and provenance must never perturb dedup folding. ``[]`` == recognized with
    # no schema prior in context (zero behaviour change downstream).
    schema_sources: list[str] = field(default_factory=list)
    # --- temporal grounding (#546 fact folding + lifecycle) -------------------
    # Stamped by ``intent.normalize.stamp_temporal`` at persist time — the
    # DETERMINISTIC resolution of ``payload.when_text`` anchored at ``ts``.
    # ``None`` == unresolvable (best-effort): the row never folds semantically
    # and never expires, i.e. pre-#546 behavior. Kept OUT of ``payload`` for
    # the same reason as ``schema_sources``: derived metadata must never
    # perturb the content ``dedup_key``.
    #   resolved_at : ISO8601 — the commitment's point in time (meeting start /
    #                 reminder deadline).
    #   valid_until : ISO8601 — resolved_at(+range end) + per-kind grace; the
    #                 daily harvest flips open rows past this to ``expired``.
    resolved_at: str | None = None
    valid_until: str | None = None

    # --- serialization -------------------------------------------------------

    def to_dict(self) -> dict:
        d = asdict(self)
        d["evidence"] = [e if isinstance(e, dict) else e.to_dict() for e in self.evidence]
        return d

    @classmethod
    def from_dict(cls, raw: dict) -> Intent:
        ev = [IntentEvidence.from_dict(e) for e in (raw.get("evidence") or [])]
        return cls(
            kind=str(raw["kind"]),
            scope=str(raw.get("scope") or ""),
            confidence=float(raw.get("confidence", 0.0)),
            rationale=str(raw.get("rationale") or ""),
            status=str(raw.get("status") or "open"),
            ts=str(raw.get("ts") or ""),
            payload=dict(raw.get("payload") or {}),
            evidence=ev,
            id=raw.get("id"),
            fire_on=str(raw.get("fire_on") or ""),
            fire_config=dict(raw.get("fire_config") or {}),
            fired_at=raw.get("fired_at"),
            schema_sources=[str(s) for s in (raw.get("schema_sources") or [])],
            resolved_at=raw.get("resolved_at"),
            valid_until=raw.get("valid_until"),
        )

    def payload_json(self) -> str:
        return json.dumps(self.payload, ensure_ascii=False)

    def evidence_json(self) -> str:
        return json.dumps([e.to_dict() for e in self.evidence], ensure_ascii=False)

    def to_text(self) -> str:
        """Compact, FTS-friendly one-liner for the markdown/`entries` projection.

        Kept human- and keyword-searchable so it lands in the same FTS index
        that ``search_memory`` / chat already query.
        """
        when = self.payload.get("when_text", "")
        people = self.payload.get("with") or []
        channel = self.payload.get("channel", "")
        bits = [f"[{self.kind}]"]
        if when:
            bits.append(f"when={when}")
        if people:
            bits.append("with=" + ",".join(str(p) for p in people))
        if channel:
            bits.append(f"via={channel}")
        if self.rationale:
            bits.append(f"— {self.rationale}")
        return " ".join(bits)
