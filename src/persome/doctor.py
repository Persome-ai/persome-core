"""``persome doctor`` — offline self-check for a bring-your-own-provider install.

Each check returns a :class:`Check` with a three-state status:

* ``ok``   — ✓ the prerequisite is satisfied.
* ``fail`` — ✗ the install is broken/incomplete; the CLI exits 1.
* ``warn`` — ⚠ inconclusive or degraded (e.g. base URL unreachable from this
  network, AX trust unknowable off-macOS); never affects the exit code.

Design constraints (BYO-key onboarding):

* **Zero LLM calls.** The only network I/O is a single HTTP ``HEAD`` against the
  configured provider endpoint (3s timeout), and even that failing is a
  warning only — a firewalled machine must still be able to get a green doctor.
* **No persistent side effects** beyond creating the data root when probing
  writability. Doctor never compiles helpers, never touches the on-disk DB,
  never writes config. Its SQLite feature probe is in-memory only.
* Secrets are never printed — key presence is reported, values are not.
"""

from __future__ import annotations

import contextlib
import os
import platform
import shutil
import socket
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import config as config_mod
from . import env_file as env_file_mod
from . import paths, runtime_pid
from .providers import ResolvedLLMProfile, resolve_profile

Status = Literal["ok", "fail", "warn"]

# HEAD probe budget for the base-URL reachability check (seconds).
_HEAD_TIMEOUT = 3.0

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


def _helper_sources(name: str) -> list[Path]:
    """Current bundled/dev Swift sources, without compiling either one."""
    sources: list[Path] = []
    try:
        from importlib.resources import files as _pkg_files

        sources.append(Path(str(_pkg_files("persome").joinpath("_bundled"))) / f"{name}.swift")
    except (ModuleNotFoundError, ValueError):
        pass
    sources.append(Path(__file__).resolve().parents[2] / "resources" / f"{name}.swift")
    return sources


def _helper_candidates(name: str) -> list[Path]:
    """Exact immutable binaries for current sources; historical cache is ignored."""
    from .capture.ax_capture import _native_binary_path

    return [
        target
        for source in _helper_sources(name)
        if source.is_file() and (target := _native_binary_path(source, name)) is not None
    ]


def _configured_helper_path(name: str, env_var: str) -> Path | None:
    override = os.environ.get(env_var)
    if override:
        candidate = Path(override).expanduser()
        return candidate if _is_executable_file(candidate) else None
    return next((path for path in _helper_candidates(name) if _is_executable_file(path)), None)


def _llm_profile() -> ResolvedLLMProfile:
    return resolve_profile(config_mod.load().model_for("default"))


def check_env_file(profile: ResolvedLLMProfile | None = None) -> Check:
    """The dotenv secret store exists and is private (0600 — no group/other bits)."""
    profile = profile or _llm_profile()
    p = paths.env_file()
    if not p.exists():
        if profile.credential_ready:
            if not profile.key_required:
                return Check(
                    "env file",
                    "warn",
                    f"{p} missing (the local LLM needs no key; rerun install.sh "
                    "to provision screenshot encryption)",
                )
            return Check(
                "env file",
                "warn",
                f"{p} missing ({profile.api_key_env} is available in this shell; "
                "writing it to the env file survives restarts)",
            )
        return Check(
            "env file",
            "fail",
            f"{p} missing — run `persome llm setup`, then rerun install.sh "
            "to provision the screenshot key",
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


def check_api_key(profile: ResolvedLLMProfile | None = None) -> Check:
    """The selected provider credential is resolvable without exposing it."""
    profile = profile or _llm_profile()
    if profile.credential_ready:
        detail = (
            "not required (local endpoint)" if not profile.key_required else profile.api_key_env
        )
        return Check("LLM credential", "ok", detail)
    return Check(
        "LLM credential",
        "fail",
        f"{profile.api_key_env} is not set for {profile.provider_label} — run `persome llm setup`",
    )


def check_screenshot_key() -> Check:
    """Machine-local AES-256 screenshot key, without ever printing its value."""
    raw = os.environ.get(env_file_mod.SCREENSHOT_KEY_ENV)
    if env_file_mod.is_valid_screenshot_key(raw):
        return Check(env_file_mod.SCREENSHOT_KEY_ENV, "ok", "set (32-byte local key)")
    return Check(
        env_file_mod.SCREENSHOT_KEY_ENV,
        "warn",
        "missing or invalid — encrypted screenshot persistence will be omitted; "
        "rerun install.sh to provision it",
    )


def check_local_api_token() -> Check:
    """Required local HTTP bearer token, without ever printing its value."""
    raw = os.environ.get(env_file_mod.LOCAL_API_TOKEN_ENV)
    if env_file_mod.is_valid_local_api_token(raw):
        return Check(env_file_mod.LOCAL_API_TOKEN_ENV, "ok", "set (owner-local bearer token)")
    return Check(
        env_file_mod.LOCAL_API_TOKEN_ENV,
        "fail",
        "missing or invalid — protected REST, viewer, and HTTP MCP are unavailable; "
        "rerun install.sh to provision it",
    )


def check_sqlite_secure_fts() -> Check:
    """SQLite must support FTS5's persistent secure-delete option (3.42+)."""
    version = sqlite3.sqlite_version
    if sqlite3.sqlite_version_info < (3, 42, 0):
        return Check(
            "SQLite secure FTS",
            "fail",
            f"SQLite {version} is too old; 3.42+ is required to erase deleted FTS text",
        )
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(":memory:")
        # Keep this ephemeral capability probe out of the static production
        # schema inventory (which intentionally scans literal CREATE TABLE DDL).
        conn.execute("CREATE VIRTUAL " + "TABLE secure_fts_probe USING fts5(content)")
        conn.execute(
            "INSERT INTO secure_fts_probe(secure_fts_probe, rank) VALUES('secure-delete', 1)"
        )
    except sqlite3.Error as exc:
        return Check(
            "SQLite secure FTS",
            "fail",
            f"SQLite {version} lacks secure FTS5 support ({exc.__class__.__name__})",
        )
    finally:
        if conn is not None:
            conn.close()
    return Check("SQLite secure FTS", "ok", f"SQLite {version}")


def check_base_url(profile: ResolvedLLMProfile | None = None) -> Check:
    """HEAD the configured provider endpoint. Reachability is a
    warning-only signal: any HTTP response counts as reachable; a network error
    warns but never fails (offline machines still get a usable doctor)."""
    profile = profile or _llm_profile()
    url = profile.base_url
    if not url:
        return Check("LLM endpoint", "fail", "missing — run `persome llm setup`")
    label = f"{profile.provider_label}: {url}"
    try:
        import httpx

        httpx.head(url, timeout=_HEAD_TIMEOUT, follow_redirects=True)
    except Exception as exc:  # noqa: BLE001 — reachability is advisory only
        return Check("LLM endpoint", "warn", f"{label}: unreachable ({exc.__class__.__name__})")
    return Check("LLM endpoint", "ok", label)


def check_helpers(capture: config_mod.CaptureConfig | None = None) -> list[Check]:
    """Check compiled helpers or the prerequisites for first-run compilation."""
    out: list[Check] = []
    for name, env_var in _HELPERS:
        if capture is not None and capture.source == "ingest":
            out.append(Check(name, "ok", "not applicable; trusted ingest producer owns capture"))
            continue
        if capture is not None and name == "mac-ax-watcher" and not capture.event_driven:
            out.append(Check(name, "ok", "not required; event-driven capture is disabled"))
            continue
        override = os.environ.get(env_var)
        if override:
            p = Path(override).expanduser()
            if _is_executable_file(p):
                out.append(Check(name, "ok", f"{p} (via {env_var})"))
            else:
                out.append(Check(name, "fail", f"{env_var}={p} is not an executable file"))
            continue
        candidates = _helper_candidates(name)
        found = next((c for c in candidates if _is_executable_file(c)), None)
        if found is not None:
            out.append(Check(name, "ok", str(found)))
        elif any(source.is_file() for source in _helper_sources(name)):
            if shutil.which("swiftc"):
                out.append(
                    Check(
                        name,
                        "warn",
                        "current Swift source found — compile it with the installer",
                    )
                )
            else:
                out.append(
                    Check(
                        name,
                        "fail",
                        "bundled Swift source found but swiftc is unavailable — "
                        "install Xcode Command Line Tools",
                    )
                )
        else:
            out.append(
                Check(
                    name,
                    "fail",
                    "binary and bundled Swift source not found — reinstall Persome",
                )
            )
    return out


def check_ax_trust(capture: config_mod.CaptureConfig | None = None) -> Check:
    """macOS Accessibility trust for the configured native capture helpers.

    Off-macOS, or when the probe itself errors, the answer is unknowable →
    ``warn`` (unknown), never a hard fail."""
    capture = capture or config_mod.CaptureConfig()
    if capture.source == "ingest":
        return Check("AX trust", "ok", "not applicable; trusted ingest producer owns capture")
    if platform.system() != "Darwin":
        return Check("AX trust", "warn", "unknown (not macOS)")
    try:
        from .capture.ax_capture import _binary_ax_trusted

        required = [_HELPERS[0]]
        if capture.event_driven:
            required.append(_HELPERS[1])
        helper_paths = [_configured_helper_path(name, env_var) for name, env_var in required]
        if any(path is None for path in helper_paths):
            return Check(
                "AX trust",
                "fail",
                "current bundled AX helper is missing — rerun the Persome installer",
            )
        trusted = all(_binary_ax_trusted(path) for path in helper_paths if path is not None)
    except Exception as exc:  # noqa: BLE001 — a TCC probe must never crash doctor
        return Check("AX trust", "warn", f"unknown (probe failed: {exc.__class__.__name__})")
    if trusted:
        principals = (
            "capture helper and event watcher" if capture.event_driven else "capture helper"
        )
        return Check("AX trust", "ok", f"Accessibility granted to the bundled {principals}")
    return Check(
        "AX trust",
        "fail",
        "not granted — System Settings → Privacy & Security → Accessibility "
        "(needed for capture.source='daemon'; 'ingest' mode does not need it)",
    )


def check_screen_recording(capture: config_mod.CaptureConfig) -> Check:
    """Check the TCC permission required by OCR and screenshot capture."""
    if capture.source == "ingest":
        return Check(
            "Screen Recording",
            "ok",
            "not applicable; trusted ingest producer owns screen capture",
        )
    if platform.system() != "Darwin":
        return Check("Screen Recording", "warn", "unknown (not macOS)")
    required = capture.enable_ocr_fallback or capture.include_screenshot
    if not required:
        return Check("Screen Recording", "ok", "not required by current capture settings")
    try:
        from .capture.screen_recording import has_screen_recording

        granted = has_screen_recording()
    except Exception as exc:  # noqa: BLE001 - TCC probes must not crash doctor
        return Check(
            "Screen Recording",
            "warn",
            f"unknown (probe failed: {exc.__class__.__name__})",
        )
    if granted:
        return Check("Screen Recording", "ok", "granted to the Persome runtime")
    status: Status = "fail" if capture.enable_ocr_fallback else "warn"
    return Check(
        "Screen Recording",
        status,
        "not granted — System Settings → Privacy & Security → Screen Recording; "
        "required for local OCR and screenshots",
    )


def check_ocr(capture: config_mod.CaptureConfig) -> Check:
    """Check OCR configuration, kill switch, runtime, models, and permission."""
    from .capture.ocr_health import inspect

    health = inspect(capture)
    if health.ready:
        return Check("local OCR", "ok", health.detail)
    if health.state == "disabled":
        return Check("local OCR", "warn", health.detail)
    if health.state == "runtime_unavailable" and platform.machine() != "arm64":
        return Check("local OCR", "warn", f"{health.detail}; AX capture remains available")
    return Check("local OCR", "fail", f"{health.state}: {health.detail}")


def check_root_writable() -> Check:
    """The data root exists (created on demand) and accepts writes."""
    root = paths.root()
    descriptor: int | None = None
    probe: str | None = None
    try:
        root = paths.ensure_private_dir(root)
        descriptor, probe = tempfile.mkstemp(prefix=".doctor-write-probe.", dir=root)
        # Unlink before writing so SIGKILL or a crash cannot leave a probe file.
        os.unlink(probe)
        probe = None
        os.write(descriptor, b"ok")
        os.fsync(descriptor)
    except (OSError, RuntimeError) as exc:
        return Check("data root writable", "fail", f"{root}: {exc}")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if probe is not None:
            with contextlib.suppress(OSError):
                os.unlink(probe)
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
    process = runtime_pid.resolve_recorded_process()
    return process.pid if process is not None else None


def run_checks(host: str, port: int) -> list[Check]:
    """Run every check in display order. Merges the env file into ``os.environ``
    first (same semantics as ``persome start``: pre-set shell vars win)."""
    env_file_mod.load_env_file(paths.env_file())
    cfg = config_mod.load()
    profile = resolve_profile(cfg.model_for("default"))
    checks: list[Check] = [
        check_env_file(profile),
        check_local_api_token(),
        check_sqlite_secure_fts(),
        check_api_key(profile),
        check_screenshot_key(),
        check_base_url(profile),
        *check_helpers(cfg.capture),
        check_ax_trust(cfg.capture),
        check_screen_recording(cfg.capture),
        check_ocr(cfg.capture),
        check_root_writable(),
        check_port(host, port),
    ]
    return checks


def has_failure(checks: list[Check]) -> bool:
    return any(c.status == "fail" for c in checks)
