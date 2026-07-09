"""Toolchain: language runtimes, package managers, editors, containers."""

from __future__ import annotations

from .base import Signal, collector, have, run_cmd

# (command, args, friendly-name)
_LANGS = [
    ("python3", ["--version"], "Python"),
    ("node", ["--version"], "Node"),
    ("go", ["version"], "Go"),
    ("rustc", ["--version"], "Rust"),
    ("java", ["-version"], "Java"),
    ("ruby", ["--version"], "Ruby"),
    ("deno", ["--version"], "Deno"),
    ("bun", ["--version"], "Bun"),
    ("swift", ["--version"], "Swift"),
    ("php", ["--version"], "PHP"),
]

_PKG_MANAGERS = ["brew", "uv", "pip", "pnpm", "yarn", "npm", "cargo", "pipx", "poetry", "conda"]
_VERSION_MGRS = ["pyenv", "nvm", "asdf", "mise", "rbenv", "fnm"]
_EDITORS = [
    ("code", "VS Code"),
    ("cursor", "Cursor"),
    ("nvim", "Neovim"),
    ("vim", "Vim"),
    ("zed", "Zed"),
    ("subl", "Sublime Text"),
    ("emacs", "Emacs"),
]
_CONTAINERS = ["docker", "podman", "colima", "kubectl", "helm"]


def _first_version_line(raw: str | None) -> str | None:
    if not raw:
        return None
    return raw.splitlines()[0].strip() or None


@collector("toolchain", "技术栈", "toolchain")
def collect() -> list[Signal]:
    signals: list[Signal] = []

    langs: list[dict[str, str]] = []
    for cmd, args, friendly in _LANGS:
        # Some tools (java, swift) print version to stderr — run_cmd captures both
        # only via stdout; fall back to the friendly name when version is hidden.
        ver = _first_version_line(run_cmd([cmd, *args]))
        if ver:
            langs.append({"name": friendly, "detail": ver})
        elif have(cmd):
            langs.append({"name": friendly, "detail": "installed"})
    if langs:
        signals.append(Signal("语言运行时", langs))

    pkg = [m for m in _PKG_MANAGERS if have(m)]
    if pkg:
        signals.append(Signal("包管理器", pkg))

    vmgr = [m for m in _VERSION_MGRS if have(m)]
    if vmgr:
        signals.append(Signal("版本管理", vmgr))

    editors = [friendly for cmd, friendly in _EDITORS if have(cmd)]
    if editors:
        signals.append(Signal("编辑器 (CLI)", editors))

    containers = [c for c in _CONTAINERS if have(c)]
    if containers:
        signals.append(Signal("容器/编排", containers))

    # Homebrew top-level installs are a strong "what does this person use" signal.
    if have("brew"):
        leaves = run_cmd(["brew", "leaves"], timeout=10.0)
        if leaves:
            items = [x for x in leaves.split() if x]
            signals.append(Signal("Homebrew 主装", items[:30], f"{len(items)} 个 leaves"))

    return signals
