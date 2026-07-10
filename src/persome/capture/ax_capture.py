"""Cross-platform-stub AX Tree capture (macOS only in v1).

Wraps the vendored `mac-ax-helper` Swift binary. Ported from Einsia-Partner's
backend/core/capture/ax_capture_service.py with Windows branch removed and
resource resolution adapted for a uv/pip-installable package.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import platform
import subprocess
from pathlib import Path
from typing import Any, Protocol

from ..logger import get
from .ax_models import AXCaptureResult

logger = get("persome.capture")

_SUBPROCESS_TIMEOUT = 10  # seconds (covers --timeout 3 + overhead)


def ax_trusted() -> bool:
    """Whether **this process** is trusted for macOS Accessibility.

    A live TCC read of the daemon's *own* grant. This is authoritative only in
    ``capture.source = "daemon"`` mode, where the daemon spawns ``mac-ax-helper`` /
    ``mac-ax-watcher`` to read the AX tree. In ``capture.source = "ingest"`` mode the
    Swift Persome app owns capture (it reads the AX tree IN-PROCESS under its own TCC
    identity and pushes frames via ``POST /captures/ingest``); the daemon then needs
    NO Accessibility grant, so this probe is irrelevant and the app's own
    ``AXIsProcessTrusted()`` is the signal that matters. (Historically the GUI was
    told NOT to read the AX tree to avoid a second TCC principal; the ingest design
    inverts that on purpose — one app-owned principal replaces the daemon's, instead
    of adding a second.)

    Returns ``False`` on non-macOS hosts or if the framework can't be loaded.
    Pure check: no options are passed, so the system shows no dialog.
    """
    if platform.system() != "Darwin":
        return False
    try:
        appservices = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        appservices.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(appservices.AXIsProcessTrusted())
    except Exception as exc:  # noqa: BLE001 — a TCC probe must never crash the daemon
        logger.warning("AXIsProcessTrusted probe failed: %s", exc)
        return False


def _strip_frame_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_frame_fields(v) for k, v in value.items() if k != "frame"}
    if isinstance(value, list):
        return [_strip_frame_fields(item) for item in value]
    return value


def _find_compatible_sdk() -> Path | None:
    """Find an SDK compatible with the installed Swift compiler.

    Swift 6.x cannot parse macOS 26 SDK's Swift interface files (they use
    ~Copyable / ~Escapable syntax that older compilers reject). When the
    default SDK is too new, fall back to the newest macOS 15 (or 14) SDK
    available under the active developer directory.
    """
    try:
        default_sdk = subprocess.run(
            ["xcrun", "--sdk", "macosx", "--show-sdk-path"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    # If default SDK looks safe (macOS 15 or earlier), use it implicitly
    if "/MacOSX15" in default_sdk or "/MacOSX14" in default_sdk:
        return None

    # Determine the active developer directory so we search the right place
    dev_dir = "/Library/Developer/CommandLineTools"
    with contextlib.suppress(FileNotFoundError, subprocess.TimeoutExpired):
        dev_dir = (
            subprocess.run(
                ["xcode-select", "-p"],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
            or dev_dir
        )

    # Collect all plausible SDK directories
    sdk_dirs: list[Path] = []
    for candidate in (
        Path(dev_dir) / "SDKs",
        Path(dev_dir) / "Platforms" / "MacOSX.platform" / "Developer" / "SDKs",
    ):
        if candidate.is_dir():
            sdk_dirs.append(candidate)

    # Also check the hard-coded CLT fallback in case xcode-select points
    # somewhere unexpected (e.g. a beta Xcode without macOS SDKs)
    clt_sdk_dir = Path("/Library/Developer/CommandLineTools/SDKs")
    if clt_sdk_dir.is_dir() and clt_sdk_dir not in sdk_dirs:
        sdk_dirs.append(clt_sdk_dir)

    if not sdk_dirs:
        return None

    def _sdk_version(path: Path) -> tuple[int, ...]:
        """Extract version tuple from 'MacOSX15.5.sdk' → (15, 5)."""
        name = path.name
        # Strip prefix and suffix
        if name.startswith("MacOSX"):
            name = name[6:]
        if name.endswith(".sdk"):
            name = name[:-4]
        try:
            return tuple(int(p) for p in name.split(".") if p)
        except ValueError:
            return (0,)

    candidates: list[Path] = []
    for sdk_dir in sdk_dirs:
        # Prefer macOS 15, fall back to macOS 14
        for pattern in ("MacOSX15.*.sdk", "MacOSX14.*.sdk"):
            candidates.extend(sdk_dir.glob(pattern))

    # Filter to directories that actually look like valid SDKs
    candidates = [
        p for p in candidates if p.is_dir() and (p / "System" / "Library" / "Frameworks").is_dir()
    ]

    if not candidates:
        return None

    # Sort by version descending so the newest compatible SDK wins
    candidates.sort(key=_sdk_version, reverse=True)
    return candidates[0]


def _maybe_compile(swift_path: Path, binary_path: Path) -> None:
    """Dev/first-run: compile the helper if missing or stale."""
    if not swift_path.is_file():
        return
    if binary_path.is_file():
        if binary_path.stat().st_mtime >= swift_path.stat().st_mtime:
            return
        logger.info("mac-ax-helper: source newer than binary, recompiling")
    else:
        logger.info("mac-ax-helper: binary missing, compiling from source")

    cache = Path("/tmp/clang-module-cache")
    cache.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["CLANG_MODULE_CACHE_PATH"] = str(cache)
    arch = "arm64" if platform.machine() in ("arm64", "aarch64") else "x86_64"
    target = f"{arch}-apple-macos12.0"

    cmd = [
        "swiftc",
        str(swift_path),
        "-o",
        str(binary_path),
        "-O",
        "-target",
        target,
        "-swift-version",
        "5",
    ]

    sdk = _find_compatible_sdk()
    if sdk is not None:
        cmd.extend(["-sdk", str(sdk)])
        logger.info("mac-ax-helper: using SDK %s", sdk)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("mac-ax-helper compile failed: %s (install Xcode CLT?)", exc)
        return
    if result.returncode != 0:
        logger.warning(
            "mac-ax-helper compile failed (%d): %s",
            result.returncode,
            result.stderr.strip()[:300],
        )


def _resolve_helper_path() -> Path | None:
    """Find or build the mac-ax-helper binary.

    Search order:
      1. PERSOME_AX_HELPER env var (absolute path)
      2. Packaged resource shipped with the wheel (_bundled/)
      3. Dev source tree (../../../resources/ relative to this file)
    """
    if platform.system() != "Darwin":
        return None

    override = os.environ.get("PERSOME_AX_HELPER") or os.environ.get(
        "MENS_CONTEXT_AX_HELPER"
    )  # Mens is the legacy name
    if override:
        p = Path(override).expanduser().resolve()
        if p.is_file() and os.access(p, os.X_OK):
            return p
        logger.warning("PERSOME_AX_HELPER set but not executable: %s", p)

    candidates: list[Path] = []

    # 1. Bundled inside the installed package (wheel ships .swift; binary built on demand)
    try:
        from importlib.resources import files as _pkg_files

        bundled_dir = Path(str(_pkg_files("persome").joinpath("_bundled")))
        candidates.append(bundled_dir / "mac-ax-helper")
    except (ModuleNotFoundError, ValueError):
        pass

    # 2. Dev source tree
    dev_root = Path(__file__).resolve().parents[3]  # .../Persome/
    candidates.append(dev_root / "resources" / "mac-ax-helper")

    for binary_path in candidates:
        swift_path = binary_path.with_suffix(".swift")
        if swift_path.is_file():
            _maybe_compile(swift_path, binary_path)
        if binary_path.is_file() and os.access(binary_path, os.X_OK):
            return binary_path

    return None


class AXProvider(Protocol):
    @property
    def available(self) -> bool: ...

    def capture_frontmost(self, *, focused_window_only: bool = True) -> AXCaptureResult | None: ...


class UnavailableAXProvider:
    def __init__(self, reason: str) -> None:
        self.reason = reason

    @property
    def available(self) -> bool:
        return False

    def capture_frontmost(self, *, focused_window_only: bool = True) -> AXCaptureResult | None:
        return None


class MacAXHelperProvider:
    """Subprocess wrapper around the vendored mac-ax-helper Swift binary."""

    def __init__(self, *, helper_path: Path, depth: int, timeout: int, raw: bool = False) -> None:
        self._helper_path = str(helper_path)
        self._depth = depth
        self._timeout = timeout
        self._raw = raw

    @property
    def available(self) -> bool:
        return True

    def capture_frontmost(self, *, focused_window_only: bool = True) -> AXCaptureResult | None:
        return self._run(focused_window_only=focused_window_only)

    def _run(
        self,
        *,
        focused_window_only: bool = False,
    ) -> AXCaptureResult | None:
        args: list[str] = [self._helper_path]
        if focused_window_only:
            args.append("--focused-window-only")
        if self._raw:
            args.append("--raw")
        if self._depth > 0:
            args.extend(["--depth", str(self._depth)])
        args.extend(["--timeout", str(self._timeout)])

        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT)
        except subprocess.TimeoutExpired:
            logger.warning("mac-ax-helper timed out after %ds", _SUBPROCESS_TIMEOUT)
            return None
        except OSError as exc:
            logger.error("Failed to run mac-ax-helper: %s", exc)
            return None

        if proc.returncode == 2:
            logger.warning(
                "Accessibility permission not granted. "
                "Grant access to your terminal in System Settings → Privacy & Security → Accessibility."
            )
            return None
        if proc.returncode != 0:
            logger.warning(
                "mac-ax-helper exited %d: %s", proc.returncode, proc.stderr.strip()[:200]
            )
            return None

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse mac-ax-helper JSON: %s", exc)
            return None

        data = _strip_frame_fields(data)
        return AXCaptureResult(
            raw_json=data,
            timestamp=data.get("timestamp", ""),
            apps=data.get("apps", []),
            metadata={
                "mode": "frontmost",
                "depth": self._depth,
                "platform": "macos",
                "raw": self._raw,
            },
        )


def create_provider(*, depth: int = 8, timeout: int = 3, raw: bool = False) -> AXProvider:
    if platform.system() != "Darwin":
        return UnavailableAXProvider(f"unsupported platform: {platform.system()}")
    helper = _resolve_helper_path()
    if helper is None:
        return UnavailableAXProvider(
            "mac-ax-helper not found. Build it: bash resources/build-mac-ax-helper.sh"
        )
    logger.info("AX capture initialized: %s", helper)
    return MacAXHelperProvider(helper_path=helper, depth=depth, timeout=timeout, raw=raw)
