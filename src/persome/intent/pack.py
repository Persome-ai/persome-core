"""Scene capability-pack interface — the §8 online-runtime contract.

Architecture §8: the online pipeline is a *generic runtime* (mechanism, stable)
that loads a *scene capability pack* (strategy, swappable). This module defines
the contract every pack implements so the runtime can drive any scene — meeting,
chat, future packs — uniformly through the scene loop:

    ① boundary   — what scene am I, when does it start/end   → ``scope_id`` / lifecycle
    ② tap        — what signal to consume, at what granularity → ``observe``
    ③ scene_state— the accumulated "this scene so far" state    → ``SceneState``
    ④ recognize  — is now the moment, and what intent           → ``recognize``
    ⑤ feedback   — surface a hint to the user                   → ``feedback``

The pack does NOT own storage or retrieval: it persists recognized intents
through the unified sink (:func:`persome.intent.sink.persist_intent`) and
pulls background through the unified recall
(:func:`persome.intent.recall.assemble_background`). That is what makes a
pack a *strategy* on top of the shared mechanism rather than another silo.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .ontology import Intent


@dataclass
class SceneState:
    """Accumulated "this scene so far" — the ③ that single-pass recognizers lack.

    Updated incrementally each recognition cycle so ④ recognize-timing reasons
    over the evolving scene, not just an isolated signal window. Kept small and
    text-serialisable so it can be injected into an LLM prompt cheaply.
    """

    scope: str = ""
    decisions: list[str] = field(default_factory=list)
    action_items: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    surfaced: list[str] = field(default_factory=list)  # hints already pushed (anti-repeat)

    def note_surfaced(self, hint: str) -> None:
        hint = hint.strip()
        if hint and hint not in self.surfaced:
            self.surfaced.append(hint)

    def merge_entities(self, names: list[str]) -> None:
        for n in names:
            n = str(n).strip()
            if n and n not in self.entities:
                self.entities.append(n)

    def to_prompt(self, *, max_chars: int = 800) -> str:
        """Compact rendering for injection as the ③ scene-context block."""
        sections: list[tuple[str, list[str]]] = [
            ("已知决策", self.decisions),
            ("行动项", self.action_items),
            ("相关方", self.entities),
            ("未决问题", self.open_questions),
        ]
        lines: list[str] = []
        for label, items in sections:
            if items:
                lines.append(f"{label}: " + "; ".join(items))
        text = "\n".join(lines)
        return text[:max_chars]


class ScenePack(ABC):
    """A pluggable scene capability pack driven by the online runtime."""

    #: human-readable pack name, e.g. "meeting".
    name: str = "scene"

    @abstractmethod
    def scope_id(self) -> str:
        """① The scene's stable identity (used as ``Intent.scope``)."""

    @abstractmethod
    def observe(self, signal: object) -> None:
        """② Feed one unit of raw signal (transcript batch, capture, message…)."""

    @abstractmethod
    def scene_state(self) -> SceneState:
        """③ The accumulated scene state."""

    @abstractmethod
    def recognize(self) -> list[Intent]:
        """④ Decide whether now is the moment and return any recognized intents.

        Implementations persist intents via the unified sink themselves (so the
        runtime stays storage-agnostic) and return them for ⑤ feedback.
        """

    @abstractmethod
    def feedback(self, intents: list[Intent]) -> None:
        """⑤ Surface hint(s) for the recognized intents to the user."""
