"""Production context-feedback analysis (the L3 data pipeline).

Reads the app-written telemetry under ``~/.persome/logs/`` —
``context-feedback.jsonl`` (one accept/dismiss/ignore/completed/failed/
manual_baseline verdict per line, paired with the recognizer's
importance×urgency and the surfaced title/prompt) and ``context-sentinel.jsonl``
(per-poll gate decisions) — and turns them into the accept↔importance
correlation + usefulness gap + gate funnel. Surfaced via ``persome feedback-report``
so a single user's real feedback is immediately readable as it accrues.
"""

from persome.feedback.report import build_report, render_text

__all__ = ["build_report", "render_text"]
