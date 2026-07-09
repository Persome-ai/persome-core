"""Shared cacheable user-profile prefix for BOTH intent recognizers.

Composes the two STABLE personalization signals — ``schema_prior`` (D2 user-
inertia, mined daily) + the ``taste`` positive prior (accepted/completed/
manual_baseline feedback, shipped #210) — into one block rendered in the
CACHED prompt prefix. This is the fix for the structural mismatch where:

* the **fast path** had NO memory at all (a plain system string, not even
  ``cache_control``) yet it's where memory has the highest leverage (fires every
  capture, user-felt latency); and
* the **slow path** put its highest-priority, most stable layer (``schema_prior``)
  in the NON-cached body (``assemble_background``), competing for the shared
  ``recall_max_chars`` budget with episodic recall — the most stable layer in the
  most volatile position, uncached.

Both paths now build this profile and render it in their cached prefix
(``system``/``user[0]``). The slow path stops passing ``schema_prior`` into
``assemble_background`` (freeing that budget for episodic recall); the fast path
goes from "plain system string" to a cached ``(system + profile)`` prefix reused
across every fast call. Priority order, volatility order, and cache order become
the same thing.

Pure: takes already-fetched schema texts + the already-rendered taste string
(callers own the conn/file reads + the per-feature gating) and just composes +
caps. Fail-open: both empty → ``""`` (caller renders no prefix block, byte-
identical to the no-profile path). See ``config.IntentRecognizerConfig.
user_profile_enabled`` for the kill-switch (OFF = byte-identical pre-layout).
"""

from __future__ import annotations

# Safety-net cap (schema is already bounded by active_schema_inferences'
# _MAX_INFERENCES; taste by max_items — so this rarely fires). Truncates at the
# last newline ≤ the cap so a profile never ends mid-line.
_CAP = 1000


def build_user_profile(*, schema_texts: list[str] | None, taste_text: str) -> str:
    """Compose the stable user-profile block. ``""`` when both inputs are empty.

    ``schema_texts``: the list of inference strings from
    ``schema_prior.active_schema_inferences_with_sources`` (already gated by
    ``schema_prior_enabled``). Rendered with the SAME header
    ``recall.assemble_background`` used (lines 239-243) — byte-identical content,
    just relocated into the cached prefix.

    ``taste_text``: the already-rendered taste prior from
    ``taste_profile.render_user_taste_profile`` (already gated by
    ``taste_profile_enabled``; carries its own header).
    """
    parts: list[str] = []
    if schema_texts:
        parts.append("# 用户惯性先验\n" + "\n".join(schema_texts))
    taste = (taste_text or "").strip()
    if taste:
        parts.append(taste)
    if not parts:
        return ""
    out = "\n\n".join(parts)
    if len(out) <= _CAP:
        return out
    # Truncate at the last newline within the cap (don't end mid-line).
    cut = out.rfind("\n", 0, _CAP)
    return out[: cut if cut > 0 else _CAP]
