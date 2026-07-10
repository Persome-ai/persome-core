"""Deterministic secret/PII scrubber for model export and durable memory.

This zero-LLM, zero-network bilingual detector catches content that must not
leave the local store: API keys, tokens, passwords, emails, phone numbers,
card-like digit runs, and absolute home-directory paths. Callers may either
reject dirty content with :func:`scan` or mask it with :func:`redact`.

Deliberately conservative + closed-form (no model): a false positive only costs
one dropped model item (bounded), a false negative leaks a secret into durable
memory (the expensive error in this domain — asymmetric-cost constitution).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Each pattern is (category, compiled regex). Order is not significant — every
# pattern is tried and all categories that match are reported.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Provider key prefixes (OpenAI/Anthropic sk-, GitHub ghp_/gho_, AWS AKIA, Google AIza, Slack xox).
    ("api_key", re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}")),
    ("api_key", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}")),
    ("api_key", re.compile(r"\bAKIA[0-9A-Z]{12,}")),
    ("api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("api_key", re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}")),
    # Bearer / Authorization tokens.
    ("token", re.compile(r"(?i)\b(?:bearer|authorization)\s*[:=]?\s+[A-Za-z0-9._\-]{16,}")),
    # JWTs (three base64url segments).
    ("token", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    # password=/pwd=/secret=… or 密码：…  (the value, not just the label).
    (
        "password",
        re.compile(r"(?i)\b(?:password|passwd|pwd|secret|token|api[_\-]?key)\b\s*[:=]\s*\S+"),
    ),
    ("password", re.compile(r"(?:密码|口令|密钥)\s*[:：=]\s*\S+")),
    # Emails.
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    # Mainland-CN mobile + international phone runs. The international arm
    # REQUIRES a leading ``+`` — without it, ``\+?`` matched any space/dash
    # grouped digit run (meeting times ``2026-06-30 14:00 到 15:30``, order
    # numbers ``1234 5678 9012``), the #389 false-positive source.
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("phone", re.compile(r"(?<!\d)\+\d[\d\s\-]{9,18}\d(?!\d)")),
    # Card-like / long ID digit runs (13–19 digits, optionally space/dash grouped).
    ("card_or_id", re.compile(r"(?<!\d)(?:\d[ \-]?){13,19}(?!\d)")),
    # Absolute per-user home paths (leaks the OS account name + local layout).
    # The username segment is non-empty ``[^/\s]+`` and does NOT require a trailing
    # ``/`` — a bare ``/Users/alice`` (username at path end, or followed by a space /
    # newline) leaks the account name just the same. Requiring the trailing slash was
    # the #398 false-negative: ``saved to /Users/alice`` / ``cwd: /home/bob`` slipped
    # both mirrored gates into durable memory.
    ("home_path", re.compile(r"/(?:Users|home)/[^/\s]+")),
    # Long opaque high-entropy blobs (≥32 base64/hex chars) — generic secret catch-all.
    # Requires a CONTIGUOUS run mixing ≥1 letter AND ≥1 digit, and drops the word
    # separators ``-``/``_`` from the class: a real key/blob is a solid alnum(+base64
    # ``+``/``/``) string, whereas the #389 false positives were pure-alpha camelCase
    # method names (no digit) and ``-``/``_``-joined package names (separator breaks
    # the run). The two look-aheads assert entropy without over-matching identifiers.
    (
        "high_entropy",
        re.compile(r"\b(?=[A-Za-z0-9+/]*[A-Za-z])(?=[A-Za-z0-9+/]*\d)[A-Za-z0-9+/]{32,}={0,2}"),
    ),
]

# A single mask per hit in redaction mode.
_MASK = "[REDACTED]"


@dataclass
class ScrubResult:
    """Outcome of a scan. ``hits`` is the set of categories that matched."""

    clean: bool
    hits: list[str] = field(default_factory=list)
    redacted: str = ""


def scan(text: str) -> ScrubResult:
    """Scan ``text`` for secrets/PII. ``clean`` is True iff NOTHING matched.

    Never raises (a bad pattern can't take down the ingest path); returns the
    distinct matched categories in ``hits`` and a masked copy in ``redacted``.
    """
    if not text:
        return ScrubResult(clean=True, hits=[], redacted=text or "")
    hits: list[str] = []
    redacted = text
    for category, rx in _PATTERNS:
        try:
            if rx.search(text):
                if category not in hits:
                    hits.append(category)
                redacted = rx.sub(_MASK, redacted)
        except re.error:  # pragma: no cover - defensive; patterns are static
            continue
    return ScrubResult(clean=not hits, hits=hits, redacted=redacted)


def is_clean(text: str) -> bool:
    """True iff ``text`` carries no detectable secret or PII."""
    return scan(text).clean


def redact(text: str) -> str:
    """Masked copy of ``text`` (for callers that prefer masking to dropping)."""
    return scan(text).redacted
