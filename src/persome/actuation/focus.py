"""Focus-borrow fallback — the LAST resort for targets the SkyLight no-steal path can't reach
(canvas/WebGL/game viewports that filter per-pid synthetic input; see `routing.bg_path_for` →
``"borrow"``). Record the user's frontmost app, briefly activate the target to perform ONE op, then
immediately restore the user's app, so the disruption is a sub-second flicker instead of a stolen
focus. Prefer AX (`press`/`setvalue`) and the SkyLight path; this is only used when both can't.

Pure-ish + injectable: `get_front` / `activate_app` are seams (live defaults shell `osascript` /
reuse `actuator.activate`), so the borrow/restore ordering is unit-testable offline.
"""

from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Callable, Iterator

from ..logger import get
from . import actuator

logger = get("persome.actuation.focus")

_GET_FRONT_OSA = 'tell application "System Events" to get bundle identifier of first process whose frontmost is true'


def frontmost_bundle_id() -> str | None:
    """Bundle id of the user's currently-frontmost app (to restore after a borrow). None on failure."""
    try:
        out = subprocess.run(
            ["osascript", "-e", _GET_FRONT_OSA], capture_output=True, timeout=3
        ).stdout.decode().strip()
        return out or None
    except (subprocess.SubprocessError, OSError):
        return None


@contextlib.contextmanager
def borrow(
    target_app: str,
    *,
    get_front: Callable[[], str | None] = frontmost_bundle_id,
    activate_app: Callable[[str], None] = lambda a: actuator.activate(a),
) -> Iterator[None]:
    """Briefly foreground `target_app`, yield for the caller's ONE op, then restore the prior front app.

    The flicker is bounded to the body. If the prior front is unknown or equals the target, the restore
    is skipped (nothing to put back). Restore runs even if the body raises.
    """
    prev = get_front()
    activate_app(target_app)
    try:
        yield
    finally:
        if prev and prev != target_app:
            with contextlib.suppress(Exception):
                activate_app(prev)
