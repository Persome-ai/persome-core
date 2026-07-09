"""Trigger mechanism — pause detection + hard cap to decide when to invoke LLM."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from .config import TriggerConfig
from .transcript import Transcript


class TriggerManager:
    """Accumulates transcripts and fires analysis when conditions are met.

    Two trigger conditions (whichever comes first):
      1. Pause: no sentence_end for pause_seconds after the last one
      2. Hard cap: max_interval_seconds since last trigger, regardless of activity
    """

    def __init__(self, config: TriggerConfig, on_trigger: Callable[[list[Transcript]], None]):
        self._config = config
        self._on_trigger = on_trigger
        self._buffer: list[Transcript] = []
        self._lock = threading.Lock()
        self._last_trigger_time = time.time()
        self._last_sentence_end_time = 0.0
        self._pause_timer: threading.Timer | None = None
        self._cap_timer: threading.Timer | None = None
        self._running = False

    def start(self) -> None:
        self._running = True
        self._last_trigger_time = time.time()
        self._reset_cap_timer()

    def on_transcript(self, transcript: Transcript) -> None:
        if not self._running:
            return

        with self._lock:
            if transcript.is_final:
                self._buffer.append(transcript)
                self._last_sentence_end_time = time.time()
                self._reset_pause_timer()

    def _reset_pause_timer(self) -> None:
        if self._pause_timer:
            self._pause_timer.cancel()
        self._pause_timer = threading.Timer(self._config.pause_seconds, self._on_pause)
        self._pause_timer.daemon = True
        self._pause_timer.start()

    def _reset_cap_timer(self) -> None:
        if self._cap_timer:
            self._cap_timer.cancel()
        self._cap_timer = threading.Timer(self._config.max_interval_seconds, self._on_cap)
        self._cap_timer.daemon = True
        self._cap_timer.start()

    def _on_pause(self) -> None:
        self._fire("pause")

    def _on_cap(self) -> None:
        self._fire("cap")

    def _fire(self, reason: str) -> None:
        with self._lock:
            if not self._buffer:
                self._reset_cap_timer()
                return
            batch = list(self._buffer)
            self._buffer.clear()
            self._last_trigger_time = time.time()

        if self._pause_timer:
            self._pause_timer.cancel()
        self._reset_cap_timer()

        self._on_trigger(batch)

    def stop(self) -> None:
        self._running = False
        if self._pause_timer:
            self._pause_timer.cancel()
        if self._cap_timer:
            self._cap_timer.cancel()
