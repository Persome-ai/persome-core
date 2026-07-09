"""Per-target routing for background (no-steal) actuation — which delivery path a coordinate/keyboard
verb should take for a given app, so the actuation layer never steals the user's cursor/focus when it
doesn't have to. Pure + unit-tested; the actual posting lives in the actuator binary + `actuator.py`.

Decided on-device (2026-06-26, macOS 26.5), see
`docs/superpowers/plans/2026-06-26-persome-background-actuation-skylight.md`:
  • AX `press`/`setvalue` need NO coordinate posting at all — always background-safe (skip routing).
  • Electron/Chromium + native AppKit coordinate clicks / keys → **skylight** (SkyLight per-pid post +
    focus-without-raise; verified to land in a backgrounded Chrome with front+cursor unchanged).
  • canvas / WebGL / game engines filter per-pid routes → **borrow** (briefly foreground, then restore).
  • skylight unavailable (probe failed / old macOS) → **postpid** for native, else **borrow**.
"""

from __future__ import annotations

# Bundle ids whose viewports filter per-pid synthetic input (OpenGL/GHOST/Metal canvases). A
# coordinate click into these only lands while frontmost → the focus-borrow fallback.
CANVAS_BUNDLES = frozenset(
    {
        "org.blenderfoundation.blender",
        "com.unity3d.UnityEditor5.x",
        "com.epicgames.UnrealEditor",
        "com.valvesoftware.steam",
    }
)


def bg_path_for(bundle_id: str | None, *, skylight_available: bool = True) -> str:
    """The no-steal delivery path for a COORDINATE/keyboard verb on `bundle_id`.

    Returns one of: ``"skylight"`` (per-pid post + focus-without-raise, no steal, works backgrounded),
    ``"borrow"`` (briefly foreground the target then restore — the flicker fallback), ``"postpid"``
    (plain per-pid post; native only, when skylight is unavailable). AX element verbs don't call this —
    they're already no-steal.
    """
    bundle = (bundle_id or "").strip()
    if bundle in CANVAS_BUNDLES:
        return "borrow"  # per-pid routes are filtered; must be frontmost
    if skylight_available:
        return "skylight"
    # Degrade: native AppKit accepts plain postToPid; everything else needs a brief foreground.
    return "postpid" if _looks_native(bundle) else "borrow"


def _looks_native(bundle_id: str) -> bool:
    """A rough native-AppKit heuristic for the degrade path: Apple's own apps + non-Electron bundles.
    Electron/Chromium apps need the SkyLight channel, so when it's unavailable they fall to borrow."""
    b = bundle_id.lower()
    if b.startswith("com.apple."):
        return True
    return not any(k in b for k in ("electron", "chrome", "chromium", "lark", "slack", "code"))


# ── instance policy: can the agent get its OWN working copy of this app? ──────────────────────────
# The deeper no-steal split (user directive 2026-06-26): even SkyLight's per-pid click can't hide the
# ~200ms flicker when an app RAISES ITS OWN window on interaction (Feishu raises on conversation-switch —
# with a coordinate click OR an AXPress; it's the app's behavior, not ours). The only way to make that
# truly invisible is to never touch the user's window at all:
#   • MULTI-instance apps → the agent spawns its OWN fresh instance on an off-screen CGVirtualDisplay
#     (`stage_strategy` → "virtual_stage") and drives THAT — verified zero-steal, no flicker.
#   • SINGLE-instance / login-bound apps → the agent must share the user's one copy, so it asks consent
#     first ("lend it for a while") then operates in place ("borrow") — the app's own raise is unavoidable.
#
# "Multi-instance" here means *the agent can do the task in a fresh instance that the user never sees*.
# Browsers are the canonical case: a fresh-`--user-data-dir` browser is a fully working browser for any
# web task (the dominant computer-use target), and it accepts `open -na` for a second process. Apps whose
# value is bound to the user's login/state (Feishu/WeChat/Slack — the agent's empty instance is useless,
# it needs the user's contacts/session) are single-instance. UNKNOWN apps default to single (conservative:
# ask to borrow rather than silently spawn a second copy the user didn't expect).
MULTI_INSTANCE_BUNDLES = frozenset(
    {
        "com.google.chrome",
        "com.google.chrome.canary",
        "org.chromium.chromium",
        "com.brave.browser",
        "com.microsoft.edgemac",
        "com.vivaldi.vivaldi",
        "company.thebrowser.browser",  # Arc
    }
)


def instance_policy(bundle_id: str | None) -> str:
    """``"multi"`` if the agent can run its own fresh instance of `bundle_id` (browsers), else ``"single"``
    (login/state-bound or unknown → must share the user's one copy). Case-insensitive on the bundle id."""
    return "multi" if (bundle_id or "").strip().lower() in MULTI_INSTANCE_BUNDLES else "single"


def stage_strategy(bundle_id: str | None, *, virtual_display_available: bool = True) -> str:
    """How the agent should obtain a working surface for `bundle_id` WITHOUT touching the user's screen.

    Returns ``"virtual_stage"`` (spawn the agent's own instance on an off-screen virtual display and drive
    that — true zero-steal, no flicker; multi-instance apps only) or ``"borrow"`` (single-instance: ask the
    user to lend their one copy, then operate it in place via the SkyLight no-steal click). Degrades to
    ``"borrow"`` when the virtual-display path is unavailable (old macOS / probe failed), so the no-steal
    guarantee never silently downgrades to a steal."""
    if virtual_display_available and instance_policy(bundle_id) == "multi":
        return "virtual_stage"
    return "borrow"
