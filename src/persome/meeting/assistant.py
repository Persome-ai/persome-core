"""Meeting assistant — receives app transcripts, stores, analyzes, pushes.

Audio capture + speech-to-text now run in the app (ScreenCaptureKit system audio,
VPIO microphone, DashScope WS). This process is the analysis half: it receives
finished transcripts over HTTP (``feed_transcript``), stores them, and runs the
trigger + LLM analysis pipeline that surfaces meeting hints.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel

from .analyzer import MeetingAnalyzer
from .config import MeetingConfig
from .store import TranscriptStore
from .transcript import Transcript
from .trigger import TriggerManager


class MeetingAssistant:
    """Receives app-side transcripts and runs the analyze-and-push pipeline."""

    def __init__(
        self,
        config: MeetingConfig | None = None,
        on_event: Callable[[dict[str, str]], None] | None = None,
        console: Console | None = None,
    ):
        self._config = config or MeetingConfig()
        self._on_event = on_event
        self._console = console or Console()
        self._store: TranscriptStore | None = None
        self._trigger: TriggerManager | None = None
        self._analyzer: MeetingAnalyzer | None = None
        self._log_file: Any = None
        self._running = False

    def run(self) -> None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        oc_root = Path.home() / ".persome"
        logs_dir = oc_root / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)

        db_path = self._config.db_path
        if not db_path:
            db_path = str(oc_root / f"meeting_{ts}.db")

        self._store = TranscriptStore(db_path)

        log_path = str(logs_dir / f"meeting_{ts}.log")
        self._log_file = Path(log_path).open("a", encoding="utf-8")  # noqa: SIM115
        self._console.print(f"[dim]Database: {db_path}[/dim]")
        self._console.print(f"[dim]Log: {log_path}[/dim]")

        self._analyzer = MeetingAnalyzer(
            llm_config=self._config.llm,
            trigger_config=self._config.trigger,
            store=self._store,
            on_push=self._on_push,
            log_file=self._log_file,
            on_event=self._on_event,
        )

        self._trigger = TriggerManager(
            config=self._config.trigger,
            on_trigger=self._on_trigger,
        )

        startup_info = (
            "Transcription: app (external)\n"
            f"LLM model: {self._config.llm.model}\n"
            f"Trigger: pause {self._config.trigger.pause_seconds}s / "
            f"cap {self._config.trigger.max_interval_seconds}s"
        )
        self._console.print(
            Panel(
                f"[bold green]Meeting Assistant Started[/bold green]\n{startup_info}",
                title="Meeting Assistant",
            )
        )
        self._write_log("=== Meeting Assistant Started ===")
        for line in startup_info.split("\n"):
            self._write_log(line)

        self._running = True
        self._trigger.start()

        try:
            while self._running:
                time.sleep(0.1)
        finally:
            self._stop()

    def feed_transcript(self, source: str, text: str, is_final: bool, sentence_id: int = 0) -> None:
        # Transcription runs in the app (DashScope WS). We receive finished
        # sentences over HTTP and push them through the analysis pipeline.
        self._on_transcript(
            Transcript(
                text=text,
                source=source,
                timestamp=time.time(),
                is_final=is_final,
                sentence_id=sentence_id,
            )
        )

    def _on_transcript(self, transcript: Transcript) -> None:
        if not transcript.is_final:
            return

        if self._store:
            self._store.save(transcript)

        if self._trigger and transcript.source == "user":
            self._trigger.on_transcript(transcript)

        label = "会议" if transcript.source == "meeting" else "用户"
        self._console.print(f"[dim][{label}][/dim] {transcript.text}")
        self._write_log(f"[{label}] {transcript.text}")
        # The app renders transcripts locally (and drops any SSE echo), so we
        # don't push them back over SSE — only analysis pushes flow that way.

    def _on_trigger(self, batch: list[Transcript]) -> None:
        if self._analyzer:
            self._analyzer.analyze(batch)

    def _on_push(self, text: str) -> None:
        self._console.print(Panel(text, title="AI Assistant", border_style="cyan"))

    def _write_log(self, msg: str) -> None:
        if self._log_file:
            ts = datetime.now().strftime("%H:%M:%S")
            self._log_file.write(f"[{ts}] {msg}\n")
            self._log_file.flush()

    def _stop(self) -> None:
        self._console.print("\n[yellow]Stopping...[/yellow]")
        if self._trigger:
            with contextlib.suppress(Exception):
                self._trigger.stop()
        if self._analyzer:
            with contextlib.suppress(Exception):
                self._analyzer.close()
        if self._store:
            with contextlib.suppress(Exception):
                self._store.close()
        if self._log_file:
            self._log_file.close()
        self._console.print("[green]Meeting session ended.[/green]")
