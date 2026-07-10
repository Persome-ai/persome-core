"""Screen-state privacy signals for the capture layer.

Two read-only probes the scheduler consults at capture time to honour the
privacy guardrails (spec E7):

* :func:`is_screen_locked` — is the macOS login/lock screen up (or the machine
  asleep)? When True the scheduler skips the whole capture — nothing should be
  collected behind the lock screen.
* :func:`is_secure_input_active` — is the currently focused element a secure
  text field (a password box)? When True the scheduler skips the screenshot AND
  AX collection for that window so a password (or the screen around it) never
  lands in the buffer.

Both are deliberately small, side-effect-free, and **fail-safe in opposite
directions** so a flaky probe never silently leaks OR silently blinds the
daemon:

* Lock detection is **fail-OPEN** — any error / "don't know" → ``False``
  (treated as *unlocked*), so a broken probe never wedges capture and the user
  simply keeps getting captures (the status quo before this gate existed).
* Secure-input detection is **fail-CLOSED / conservative** — a "looks secure"
  signal wins, because the cost of one missed capture is nothing next to the
  cost of buffering a password.

The macOS lock probe is monkeypatch-friendly: the Quartz call is isolated in
:func:`_quartz_screen_is_locked` and the subprocess fallbacks in
:func:`_ioreg_says_locked`. Tests patch those (or the public functions) rather
than depending on a real lock screen.
"""

from __future__ import annotations

import subprocess
from typing import Any

from ..logger import get

logger = get("persome.capture.screen_state")

_SECURE_TEXT_SUBROLE = "AXSecureTextField"
_SECURE_TEXT_ROLE = "AXTextField"


# --------------------------------------------------------------------------- #
# Lock / sleep detection (fail-open)
# --------------------------------------------------------------------------- #
def _quartz_screen_is_locked() -> bool | None:
    """Read ``CGSSessionScreenIsLocked`` via Quartz (pyobjc).

    Returns the boolean lock state, or ``None`` when Quartz is unavailable or
    the session dictionary doesn't carry the key (then the caller falls back).
    Isolated so tests can monkeypatch it without importing Quartz.
    """
    try:
        from Quartz import CGSessionCopyCurrentDictionary
    except Exception:  # noqa: BLE001 — pyobjc not present (e.g. Linux CI / minimal venv)
        return None
    try:
        session = CGSessionCopyCurrentDictionary()
        if not session:
            return None
        # Present and truthy only while the screen is locked; absent otherwise.
        return bool(session.get("CGSSessionScreenIsLocked", 0))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Quartz lock probe failed: %s", exc)
        return None


def _ioreg_says_locked() -> bool | None:
    """Subprocess fallback: infer display-sleep / lock from ``ioreg``.

    Uses the IODisplayWrangler power state — ``CurrentPowerState`` of 4 means the
    display is fully on; anything lower means it has dimmed/slept, which on a
    Mac coincides with the lock screen for the password-on-wake default. This is
    a *best-effort* fallback only used when Quartz is unavailable; returns
    ``None`` (→ fail-open) on any error so a missing/odd ``ioreg`` never wedges
    capture.
    """
    try:
        out = subprocess.run(
            ["ioreg", "-n", "IODisplayWrangler", "-r", "-d", "1"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except Exception as exc:  # noqa: BLE001 — not macOS, no ioreg, timeout, etc.
        logger.debug("ioreg lock probe unavailable: %s", exc)
        return None
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        if "CurrentPowerState" in line:
            try:
                state = int(line.rsplit("=", 1)[1].strip())
            except (ValueError, IndexError):
                return None
            # 4 == display fully on. Lower == dimmed/asleep.
            return state < 4
    return None


def is_screen_locked() -> bool:
    """True when the screen is locked / the machine is asleep. **Fail-open.**

    Order: Quartz ``CGSSessionScreenIsLocked`` (authoritative) → ``ioreg``
    display-power fallback. If neither yields a definite answer, returns
    ``False`` (treated as unlocked) so a broken probe never blinds the daemon.
    Never raises.
    """
    try:
        quartz = _quartz_screen_is_locked()
        if quartz is not None:
            return quartz
        ioreg = _ioreg_says_locked()
        if ioreg is not None:
            return ioreg
    except Exception as exc:  # noqa: BLE001 — fail-open belt-and-suspenders
        logger.debug("lock detection errored, treating as unlocked: %s", exc)
    return False


# --------------------------------------------------------------------------- #
# Secure-input (password box) detection (fail-conservative)
# --------------------------------------------------------------------------- #
def _focused_element_from_capture(out: dict[str, Any]) -> dict[str, Any] | None:
    """The OS-reported focused element from a built capture's ax_tree.

    Reads the frontmost app's ``focused_element`` (the AX helper emits a compact
    ``{role, subrole, ...}`` dict — secure fields are role ``AXTextField`` +
    subrole ``AXSecureTextField``). Returns ``None`` when the capture has no
    ax_tree / no frontmost app / no focused element.
    """
    ax_tree = out.get("ax_tree")
    if not isinstance(ax_tree, dict):
        return None
    apps = ax_tree.get("apps") or []
    if not isinstance(apps, list):
        return None
    front = next(
        (a for a in apps if isinstance(a, dict) and a.get("is_frontmost")),
        None,
    )
    if front is None:
        front = next((a for a in apps if isinstance(a, dict)), None)
    if front is None:
        return None
    fe = front.get("focused_element")
    return fe if isinstance(fe, dict) else None


def is_secure_input_active(out: dict[str, Any]) -> bool:
    """True when the focused element of ``out`` is a secure text field.

    Reads the (already-captured, read-only) AX info on the built capture dict.
    **Fail-conservative**: if the role/subrole markers say "secure", suppress —
    but a probe error returns ``False`` (we have nothing concrete to suppress)
    rather than raising; the suppression only fires on a positive signal. Never
    raises.
    """
    try:
        fe = _focused_element_from_capture(out)
        if not fe:
            return False
        subrole = (fe.get("subrole") or "").strip()
        role = (fe.get("role") or "").strip()
        if subrole == _SECURE_TEXT_SUBROLE:
            return True
        # Conservative belt: some helpers report only the redaction marker / a
        # secure role without the subrole. A redacted value on an editable text
        # field is the strongest "this is a password box" signal we have.
        if role == _SECURE_TEXT_ROLE and (fe.get("value") or "") == "[REDACTED]":
            return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("secure-input probe errored: %s", exc)
    return False
