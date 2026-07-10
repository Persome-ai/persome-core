"""macOS LaunchAgent integration for a launchd-owned daemon process.

Design (issue #194):

- launchd becomes the *owner* of the daemon. The plist runs the daemon in the
  foreground (``persome start --foreground``); ``KeepAlive`` makes launchd
  relaunch it whenever it exits (crash, ``stop``, OOM). Lifecycle ownership stays
  in one place instead of an embedding product spawning a competing daemon.
- stdout/stderr are routed into ``logs/launchd.{out,err}.log`` under the data root
  so the diagnostic bundle (#168), which globs ``logs/``, picks them up unchanged.
- The plist itself must live in ``~/Library/LaunchAgents/`` — launchd only scans
  that directory for per-user agents. Everything else (label, log sinks) honours
  ``PERSOME_ROOT`` so tests stay hermetic.

This module is the single source of truth for the label, plist location, and
``launchctl`` invocations.
"""

from __future__ import annotations

import os
import plistlib
import signal
import subprocess
import time
from pathlib import Path

from . import paths

#: launchd job label.
LABEL = "com.persome.runtime"

#: Labels this agent shipped under before the Persome rename. ``install()``
#: boots these out and removes their plists so an upgraded machine never keeps
#: a second copy of the daemon alive under an old name. Product-specific
#: labels belong to the consumer (see CLAUDE.md); consumers with their own
#: legacy launchd labels clean those up themselves.
LEGACY_LABELS: tuple[str, ...] = ()


def plist_path() -> Path:
    """Absolute path to the agent plist. Always under ``~/Library/LaunchAgents``
    — launchd does not scan ``PERSOME_ROOT``."""
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def gui_domain_target() -> str:
    """``gui/<uid>/<label>`` service target used by modern ``launchctl`` verbs."""
    return f"gui/{os.getuid()}/{LABEL}"


def build_plist(binary: str) -> dict[str, object]:
    """Return the plist dict for the daemon, parameterised by the daemon
    ``binary`` path (the bundled ``persome`` executable).

    ``--foreground`` keeps the process in launchd's control group (no
    double-fork); ``KeepAlive=true`` provides crash-relaunch; ``RunAtLoad=true``
    starts it as soon as the agent is bootstrapped and on every login.
    """
    env: dict[str, str] = {}
    # Propagate the data-root override so a test/dev launchd job and the CLI
    # that registered it agree on where state lives.
    root_override = os.environ.get("PERSOME_ROOT")
    if root_override:
        env["PERSOME_ROOT"] = root_override

    plist: dict[str, object] = {
        "Label": LABEL,
        "ProgramArguments": [binary, "start", "--foreground"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(paths.launchd_stdout_log()),
        "StandardErrorPath": str(paths.launchd_stderr_log()),
    }
    if env:
        plist["EnvironmentVariables"] = env
    return plist


def write_plist(binary: str) -> Path:
    """Render the plist for ``binary`` and write it to [plist_path]. Returns the
    path written."""
    target = plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    paths.logs_dir().mkdir(parents=True, exist_ok=True)
    with target.open("wb") as fh:
        plistlib.dump(build_plist(binary), fh)
    return target


def is_loaded() -> bool:
    """True iff launchd currently has the job registered (loaded), regardless
    of whether the process is up at this instant."""
    result = subprocess.run(
        ["launchctl", "print", gui_domain_target()],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def bootstrap() -> subprocess.CompletedProcess[str]:
    """Load the agent into the user's GUI domain. Idempotent-ish: launchd
    returns non-zero if already bootstrapped, which callers may ignore."""
    return subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path())],
        capture_output=True,
        text=True,
        check=False,
    )


def bootout() -> subprocess.CompletedProcess[str]:
    """Unload the agent from the user's GUI domain. Non-zero when not loaded."""
    return subprocess.run(
        ["launchctl", "bootout", gui_domain_target()],
        capture_output=True,
        text=True,
        check=False,
    )


def _terminate_stray_daemon(timeout: float = 2.0) -> None:
    """SIGTERM a *live* daemon recorded in the pid file before we start a fresh
    one, then wait (up to ``timeout``) for it to exit.

    Two cases this covers, both of which would otherwise make the new daemon's
    ``start`` bail with "Already running (pid …)":

    1. **Orphan from a pre-launchd version** — old builds double-forked the
       daemon into the background (not under launchd). ``bootout()`` only stops
       the launchd job, so on app upgrade that orphan keeps running with its
       live pid in the pid file. Kill it so launchd's ``RunAtLoad`` start can
       take over (this is the "new dmg cleans up the old daemon" path).
    2. **Slow-to-die launchd daemon** — right after ``bootout()`` the old
       process may still be shutting down; waiting here avoids a port race with
       the daemon we're about to bootstrap.

    A stale/dead pid is ignored (``start`` already tolerates it via the
    liveness check in ``cli._read_pid``). Best-effort — never raises."""
    try:
        pid = int(paths.pid_file().read_text().strip())
    except (FileNotFoundError, ValueError):
        return
    try:
        os.kill(pid, 0)  # probe liveness; ProcessLookupError ⇒ already gone
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)


def _bootout_legacy_labels() -> None:
    """Best-effort cleanup of pre-rename launchd agents (see ``LEGACY_LABELS``).

    Boots each legacy job out of the GUI domain and removes its plist. The
    plist directory is derived from ``plist_path()`` so tests that redirect the
    plist location never touch the real ``~/Library/LaunchAgents``."""
    for label in LEGACY_LABELS:
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
            check=False,
        )
        legacy_plist = plist_path().parent / f"{label}.plist"
        try:
            if legacy_plist.exists():
                legacy_plist.unlink()
        except OSError:
            pass


def install(binary: str) -> Path:
    """Write the plist and bootstrap it. Returns the plist path. If the agent is
    already loaded, it is booted out first so the new ProgramArguments (e.g. a
    fresh binary path after an app upgrade) take effect. Any stray daemon (a
    pre-launchd orphan, or the just-booted-out one still exiting) is terminated
    first so the fresh ``start`` won't bail with "Already running"."""
    _bootout_legacy_labels()
    if is_loaded():
        bootout()
    _terminate_stray_daemon()
    target = write_plist(binary)
    bootstrap()
    return target


def uninstall() -> None:
    """Boot the agent out (if loaded) and remove the plist file."""
    if is_loaded():
        bootout()
    target = plist_path()
    if target.exists():
        target.unlink()
