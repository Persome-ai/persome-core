"""System environment: OS, hardware, locale, language, timezone, machine name."""

from __future__ import annotations

import getpass
import socket
from pathlib import Path

from .base import Signal, collector, run_cmd


def _timezone() -> str | None:
    # /etc/localtime is a symlink into .../zoneinfo/<Area>/<City> on macOS.
    try:
        target = Path("/etc/localtime").resolve()
        parts = target.parts
        if "zoneinfo" in parts:
            i = parts.index("zoneinfo")
            return "/".join(parts[i + 1 :]) or None
    except OSError:
        pass
    return None


def _defaults_global(key: str) -> str | None:
    return run_cmd(["defaults", "read", "-g", key])


@collector("system", "系统环境", "system")
def collect() -> list[Signal]:
    signals: list[Signal] = []

    signals.append(Signal("用户名", getpass.getuser()))

    name = run_cmd(["scutil", "--get", "ComputerName"]) or socket.gethostname()
    if name:
        signals.append(Signal("机器名", name))

    product = run_cmd(["sw_vers", "-productName"]) or "macOS"
    version = run_cmd(["sw_vers", "-productVersion"])
    build = run_cmd(["sw_vers", "-buildVersion"])
    if version:
        os_str = f"{product} {version}" + (f" ({build})" if build else "")
        signals.append(Signal("操作系统", os_str))

    model = run_cmd(["sysctl", "-n", "hw.model"])
    chip = run_cmd(["sysctl", "-n", "machdep.cpu.brand_string"])
    if model or chip:
        hw = " / ".join(x for x in (model, chip) if x)
        signals.append(Signal("硬件", hw))

    locale = _defaults_global("AppleLocale")
    if locale:
        signals.append(Signal("Locale", locale))

    langs = _defaults_global("AppleLanguages")
    if langs:
        # plist array prints multi-line; flatten to a compact list.
        flat = " ".join(
            tok.strip().strip('",') for tok in langs.replace("(", "").replace(")", "").split()
        ).strip()
        if flat:
            signals.append(Signal("系统语言", flat))

    tz = _timezone()
    if tz:
        signals.append(Signal("时区", tz))

    return signals
