"""``persome doctor`` — offline self-check for a bring-your-own-key install.

Each check returns a :class:`Check` with a three-state status:

* ``ok``   — ✓ the prerequisite is satisfied.
* ``fail`` — ✗ the install is broken/incomplete; the CLI exits 1.
* ``warn`` — ⚠ inconclusive or degraded (e.g. base URL unreachable from this
  network, AX trust unknowable off-macOS); never affects the exit code.

Design constraints (BYO-key onboarding):

* **Zero LLM calls.** The only network I/O is a single HTTP ``HEAD`` against the
  configured ``ANTHROPIC_BASE_URL`` (3s timeout), and even that failing is a
  warning only — a firewalled machine must still be able to get a green doctor.
* **No side effects** beyond creating the data root when probing writability.
  Doctor never compiles helpers, never touches the DB, never writes config.
* Secrets are never printed — key presence is reported, values are not.
"""

from __future__ import annotations

import os
import platform
import socket
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import env_file as env_file_mod
from . import paths

Status = Literal["ok", "fail", "warn"]

# HEAD probe budget for the base-URL reachability check (seconds).
_HEAD_TIMEOUT = 3.0

# Default official endpoint when ANTHROPIC_BASE_URL is unset.
_DEFAULT_BASE_URL = "https://api.anthropic.com"

# Swift helper binaries the capture pipeline shells out to, with their
# path-override env vars (mirrors capture/ax_capture.py + capture/watcher.py).
_HELPERS: tuple[tuple[str, str], ...] = (
    ("mac-ax-helper", "PERSOME_AX_HELPER"),
    ("mac-ax-watcher", "PERSOME_AX_WATCHER"),
)


@dataclass
class Check:
    name: str
    status: Status
    detail: str = ""


def _is_executable_file(p: Path) -> bool:
    return p.is_file() and os.access(p, os.X_OK)


def _helper_candidates(name: str) -> list[Path]:
    """Candidate binary locations, in the same order the capture pipeline
    resolves them (env override → bundled wheel resource → dev source tree).
    Existence check only — doctor never triggers the on-demand compile."""
    candidates: list[Path] = []
    try:
        from importlib.resources import files as _pkg_files

        candidates.append(Path(str(_pkg_files("persome").joinpath("_bundled"))) / name)
    except (ModuleNotFoundError, ValueError):
        pass
    # Dev source tree: src/persome/doctor.py → parents[2] == repo root.
    candidates.append(Path(__file__).resolve().parents[2] / "resources" / name)
    return candidates


def check_env_file() -> Check:
    """The dotenv secret store exists and is private (0600 — no group/other bits)."""
    p = paths.env_file()
    if not p.exists():
        if os.environ.get("ANTHROPIC_API_KEY"):
            return Check(
                "env file",
                "warn",
                f"{p} missing (ANTHROPIC_API_KEY is exported in this shell; "
                "writing it to the env file survives restarts)",
            )
        return Check(
            "env file",
            "fail",
            f"{p} missing — create it with ANTHROPIC_API_KEY=sk-... then chmod 600",
        )
    try:
        mode = stat.S_IMODE(p.stat().st_mode)
    except OSError as exc:
        return Check("env file", "fail", f"{p}: stat failed: {exc}")
    if mode & 0o077:
        return Check(
            "env file",
            "fail",
            f"{p} is mode {mode:o} — secrets file must be private: chmod 600 {p}",
        )
    return Check("env file", "ok", str(p))


def check_api_key() -> Check:
    """``ANTHROPIC_API_KEY`` resolvable (env file already merged by run_checks)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return Check("ANTHROPIC_API_KEY", "ok", "set")
    return Check(
        "ANTHROPIC_API_KEY",
        "fail",
        f"not set — add ANTHROPIC_API_KEY=... to {paths.env_file()} "
        "(official Anthropic key, or a compatible gateway's key with ANTHROPIC_BASE_URL)",
    )


def check_base_url() -> Check:
    """HEAD the configured (or default) Anthropic base URL. Reachability is a
    warning-only signal: any HTTP response counts as reachable; a network error
    warns but never fails (offline machines still get a usable doctor)."""
    base = os.environ.get("ANTHROPIC_BASE_URL") or ""
    url = base or _DEFAULT_BASE_URL
    label = url if base else f"{url} (default)"
    try:
        import httpx

        httpx.head(url, timeout=_HEAD_TIMEOUT, follow_redirects=True)
    except Exception as exc:  # noqa: BLE001 — reachability is advisory only
        return Check("base URL", "warn", f"{label}: unreachable ({exc.__class__.__name__})")
    return Check("base URL", "ok", label)


def check_helpers() -> list[Check]:
    """The Swift capture helpers are compiled (binary present + executable)."""
    out: list[Check] = []
    for name, env_var in _HELPERS:
        override = os.environ.get(env_var)
        if override:
            p = Path(override).expanduser()
            if _is_executable_file(p):
                out.append(Check(name, "ok", f"{p} (via {env_var})"))
            else:
                out.append(Check(name, "fail", f"{env_var}={p} is not an executable file"))
            continue
        found = next((c for c in _helper_candidates(name) if _is_executable_file(c)), None)
        if found is not None:
            out.append(Check(name, "ok", str(found)))
        else:
            out.append(
                Check(
                    name,
                    "fail",
                    "binary not found — run install.sh (or bash scripts inside "
                    "resources/) to compile the Swift helpers",
                )
            )
    return out


def check_ax_trust() -> Check:
    """macOS Accessibility trust for THIS process (capture.source='daemon' mode).

    Off-macOS, or when the probe itself errors, the answer is unknowable →
    ``warn`` (unknown), never a hard fail."""
    if platform.system() != "Darwin":
        return Check("AX trust", "warn", "unknown (not macOS)")
    try:
        from .capture.ax_capture import ax_trusted

        trusted = ax_trusted()
    except Exception as exc:  # noqa: BLE001 — a TCC probe must never crash doctor
        return Check("AX trust", "warn", f"unknown (probe failed: {exc.__class__.__name__})")
    if trusted:
        return Check("AX trust", "ok", "Accessibility granted to this process")
    return Check(
        "AX trust",
        "fail",
        "not granted — System Settings → Privacy & Security → Accessibility "
        "(needed for capture.source='daemon'; 'ingest' mode does not need it)",
    )


def check_root_writable() -> Check:
    """The data root exists (created on demand) and accepts writes."""
    root = paths.root()
    probe = root / ".doctor-write-probe"
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return Check("data root writable", "fail", f"{root}: {exc}")
    return Check("data root writable", "ok", str(root))


def check_port(host: str, port: int) -> Check:
    """The daemon port is either free, or already held by OUR running daemon."""
    pid = _running_daemon_pid()
    if pid is not None:
        return Check("port", "ok", f"{host}:{port} in use by the running daemon (pid {pid})")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
    except OSError as exc:
        return Check(
            "port",
            "fail",
            f"{host}:{port} is not bindable ({exc}) — another process holds it; "
            "change [mcp] port in config.toml or stop the other process",
        )
    return Check("port", "ok", f"{host}:{port} free")


def _running_daemon_pid() -> int | None:
    try:
        pid = int(paths.pid_file().read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


def run_checks(host: str, port: int) -> list[Check]:
    """Run every check in display order. Merges the env file into ``os.environ``
    first (same semantics as ``persome start``: pre-set shell vars win)."""
    env_file_mod.load_env_file(paths.env_file())
    checks: list[Check] = [
        check_env_file(),
        check_api_key(),
        check_base_url(),
        *check_helpers(),
        check_ax_trust(),
        check_root_writable(),
        check_port(host, port),
    ]
    return checks


def has_failure(checks: list[Check]) -> bool:
    return any(c.status == "fail" for c in checks)
