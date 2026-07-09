"""Meeting as the first concrete :class:`ScenePack`.

This adapter is what keeps the §8 ScenePack contract from being a paper
abstraction: it drives the *existing* :class:`MeetingAnalyzer` through the
generic ①-⑤ scene loop, while recognition results flow through the **unified**
intent stream rather than the analyzer's old push-only island.

The analyzer recognizes asynchronously (LLM in a worker thread, hints surfaced
via its ``on_push`` callback and persisted into the unified store as they land).
This pack is therefore a *synchronous façade* over that engine:

    ① scope_id    → the analyzer's per-meeting scope
    ② observe     → hand a transcript batch to the analyzer's async recognition
    ③ scene_state → the analyzer's accumulated :class:`SceneState`
    ④ recognize   → read back this scene's slice of the unified intent stream
    ⑤ feedback    → meeting surfaces inline during ②; here we just keep the
                    scene's anti-repeat set warm

Reading ④ from the shared store (not from analyzer-private buffers) is the point:
the meeting is no longer a silo — any consumer of the unified stream sees its
recognitions the same way it sees timeline/chat ones.
"""

from __future__ import annotations

from ..intent import store as intent_store
from ..intent.ontology import Intent
from ..intent.pack import ScenePack, SceneState
from ..store import fts
from .analyzer import MeetingAnalyzer
from .transcript import Transcript


class MeetingScenePack(ScenePack):
    """Synchronous ScenePack façade over the async :class:`MeetingAnalyzer`."""

    name = "meeting"

    def __init__(self, analyzer: MeetingAnalyzer):
        self._analyzer = analyzer
        # High-water mark so ④ recognize() returns only intents recognized since
        # the previous call — the scene's *fresh* recognitions, not its history.
        self._last_seen_ts = ""

    def scope_id(self) -> str:
        return self._analyzer.scope

    def observe(self, signal: object) -> None:
        if not isinstance(signal, list):
            raise TypeError("meeting pack observes a list[Transcript] batch")
        batch: list[Transcript] = signal
        self._analyzer.analyze(batch)

    def scene_state(self) -> SceneState:
        return self._analyzer.scene

    def recognize(self) -> list[Intent]:
        with fts.cursor() as conn:
            recognized = intent_store.intents_for_scope(conn, self.scope_id())
        fresh = [i for i in recognized if i.ts > self._last_seen_ts]
        if fresh:
            self._last_seen_ts = max(i.ts for i in fresh)
        return fresh

    def feedback(self, intents: list[Intent]) -> None:
        # Hints are surfaced inline during ② (analyzer.on_push); nothing extra to
        # push here. Keep the scene's anti-repeat set warm so a future recognizer
        # cycle won't re-surface what's already been shown.
        for intent in intents:
            text = str(intent.payload.get("text", ""))
            if text:
                self._analyzer.scene.note_surfaced(text)
