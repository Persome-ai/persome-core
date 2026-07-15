"""Cross-platform-stub AX Tree capture (macOS only in v1).

Wraps the vendored `mac-ax-helper` Swift binary. Ported from Einsia-Partner's
backend/core/capture/ax_capture_service.py with Windows branch removed and
resource resolution adapted for a uv/pip-installable package.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import platform
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Protocol

from .. import paths
from ..logger import get
from .ax_models import AXCaptureResult

logger = get("persome.capture")

_SUBPROCESS_TIMEOUT = 10  # seconds (covers --timeout 3 + overhead)
_AX_TRUST_CACHE_SECONDS = 1.0
_ax_trust_cache: dict[bool, tuple[float, bool]] = {}
_ax_trust_lock = threading.Lock()


def _binary_ax_trusted(binary: Path) -> bool:
    try:
        result = subprocess.run(
            [str(binary), "--check-accessibility"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def ax_trusted(*, refresh: bool = False, include_watcher: bool = True) -> bool:
    """Whether every native AX principal used by this capture policy is trusted."""

    if platform.system() != "Darwin":
        return False
    now = time.monotonic()
    with _ax_trust_lock:
        if (
            not refresh
            and include_watcher in _ax_trust_cache
            and now - _ax_trust_cache[include_watcher][0] < _AX_TRUST_CACHE_SECONDS
        ):
            return _ax_trust_cache[include_watcher][1]

    helper_path = _resolve_helper_path()
    trusted = bool(helper_path is not None and _binary_ax_trusted(helper_path))
    if trusted and include_watcher:
        from . import watcher

        watcher_path = watcher._resolve_watcher_path()
        trusted = bool(watcher_path is not None and _binary_ax_trusted(watcher_path))
    with _ax_trust_lock:
        _ax_trust_cache[include_watcher] = (now, trusted)
    return trusted


def request_accessibility_permission(*, include_watcher: bool = True) -> bool:
    """Prompt/register exactly the helper binaries used by the capture policy."""

    if platform.system() != "Darwin":
        return False
    binaries: list[Path | None] = [_resolve_helper_path()]
    if include_watcher:
        from . import watcher

        binaries.append(watcher._resolve_watcher_path())
    requested = False
    for binary in binaries:
        if binary is None:
            continue
        try:
            result = subprocess.run(
                [str(binary), "--request-accessibility"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
            requested = requested or result.returncode == 0
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("Accessibility permission request failed for %s: %s", binary, exc)
    with _ax_trust_lock:
        _ax_trust_cache.clear()
    return requested


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
    helper_name = binary_path.name
    if not swift_path.is_file():
        return
    if binary_path.is_file():
        if binary_path.stat().st_mtime >= swift_path.stat().st_mtime:
            return
        logger.info("%s: source newer than binary, recompiling", helper_name)
    else:
        logger.info("%s: binary missing, compiling from source", helper_name)

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
        logger.info("%s: using SDK %s", helper_name, sdk)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("%s compile failed: %s (install Xcode CLT?)", helper_name, exc)
        return
    if result.returncode != 0:
        logger.warning(
            "%s compile failed (%d): %s",
            helper_name,
            result.returncode,
            result.stderr.strip()[:300],
        )


def _native_source_digest(swift_path: Path) -> str | None:
    """Return the architecture-bound identity for one native helper source."""
    try:
        return hashlib.sha256(
            b"persome-native-helper-v1\0"
            + platform.machine().encode("utf-8")
            + b"\0"
            + swift_path.read_bytes()
        ).hexdigest()
    except OSError:
        return None


def _native_binary_path(swift_path: Path, name: str) -> Path | None:
    """Expected immutable machine-local path for this exact helper source."""
    digest = _native_source_digest(swift_path)
    return paths.native_helpers_dir() / digest / name if digest is not None else None


def _stable_native_binary(swift_path: Path, name: str) -> Path | None:
    """Compile once into an immutable, source-versioned TCC-visible path.

    Swift's ad-hoc linker signature can get a different CDHash on every build.
    Recompiling inside each replacement venv would therefore invalidate an
    otherwise valid Accessibility grant. Immutable per-source copies let same-
    version reinstalls reuse the exact binary and let a failed update return to
    the old binary byte-for-byte.
    """
    target = _native_binary_path(swift_path, name)
    if target is None:
        return None
    digest = target.parent.name

    root = paths.ensure_private_dir(paths.native_helpers_dir())
    directory = paths.ensure_private_dir(root / digest)
    lock = paths.open_private_lock_file(root / ".build.lock")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if target.is_symlink():
            raise RuntimeError("native AX helper paths must not be symlinks")
        if target.is_file() and os.access(target, os.X_OK):
            return target

        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{name}.", dir=directory)
        os.close(descriptor)
        temporary = Path(temporary_name)
        temporary.unlink()
        try:
            _maybe_compile(swift_path, temporary)
            if not temporary.is_file() or not os.access(temporary, os.X_OK):
                return None
            os.chmod(temporary, 0o700)
            binary_fd = os.open(temporary, os.O_RDONLY)
            try:
                os.fsync(binary_fd)
            finally:
                os.close(binary_fd)
            os.replace(temporary, target)
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return target
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def _resolve_helper_path() -> Path | None:
    """Find or build the mac-ax-helper binary.

    Search order:
      1. PERSOME_AX_HELPER env var (absolute path)
      2. Packaged resource shipped with the wheel (_bundled/)
      3. Dev source tree (../../../resources/ relative to this file)
    """
    if platform.system() != "Darwin":
        return None

    override = os.environ.get("PERSOME_AX_HELPER")
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
        candidates.append(bundled_dir / "mac-ax-helper.swift")
    except (ModuleNotFoundError, ValueError):
        pass

    # 2. Dev source tree
    dev_root = Path(__file__).resolve().parents[3]  # .../Persome/
    candidates.append(dev_root / "resources" / "mac-ax-helper.swift")

    for swift_path in candidates:
        if swift_path.is_file():
            binary_path = _stable_native_binary(swift_path, "mac-ax-helper")
            if binary_path is not None:
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
                "Run `persome onboard` to grant mac-ax-helper in System Settings → "
                "Privacy & Security → Accessibility."
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
