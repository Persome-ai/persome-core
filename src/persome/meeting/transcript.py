"""Transcript — the one data model shared across the meeting analysis pipeline.

Speech-to-text now runs in the app (ScreenCaptureKit + VPIO mic + DashScope WS);
this process receives finished transcripts over HTTP (see
``MeetingAssistant.feed_transcript``) and runs them through storage + trigger +
analysis. This module holds just the value object those stages pass around.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Transcript:
    text: str
    source: str  # "meeting" or "user"
    timestamp: float
    is_final: bool
    sentence_id: int = 0
