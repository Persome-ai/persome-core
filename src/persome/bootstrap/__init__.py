"""Cold-start context harvester (day-0 bootstrap).

Persome's normal pipeline accretes context *over time* from screen
capture. The moment a user installs the app, that history is empty — the
app is blind. This package榨干 (exhausts) what the local machine already
knows about the person *before any capture exists*, so day 0 isn't a blank
slate.

Design tenets (see repo CLAUDE.md "设计哲学"):

- **Asymmetric cost rules the shape.** Missing a signal is a bounded loss
  (later capture backfills it). Leaking something private — to the LLM or
  onto the terminal — is a compounding loss (an investor sees it once and
  trust is gone). So the battle is fought on the presentation side and on
  *what leaves the machine*, not on "harvest everything possible".
- **Local exhaustion = cognitive entropy.** Everything readable on the
  machine (git identity, command habits, installed apps) is knowable; pile
  on collectors and it drops. This part we do in full.
- **The LLM only eats a condensed summary**, never raw data. Collectors
  emit aggregates and metadata (top domains, command frequencies, app
  names, project names) — never raw browser-history rows or file contents.
  ``redactor`` caps and tidies that before ``synthesizer`` sends it on.

Entry point: ``persome bootstrap`` (see ``runner.run``).
"""

from __future__ import annotations
