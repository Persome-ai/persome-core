"""Shell habits: command frequencies from history, aliases & tooling from rc."""

from __future__ import annotations

import os
import re

from .base import Signal, collector, home, read_text, top_counts

# Generic leading tokens that say nothing about what the person does.
_BORING = {"sudo", "cd", "ls", "ll", "la", "clear", "exit", "pwd", "echo", "cat", "which", "man"}


def _history_file() -> str | None:
    shell = os.environ.get("SHELL", "")
    candidates = []
    if "zsh" in shell:
        candidates = [".zsh_history", ".zhistory", ".bash_history"]
    else:
        candidates = [".bash_history", ".zsh_history"]
    for name in candidates:
        p = home() / name
        if p.exists():
            return name
    return None


def _parse_zsh_line(line: str) -> str:
    # zsh extended history: ": <epoch>:<elapsed>;<command>"
    if line.startswith(":") and ";" in line:
        return line.split(";", 1)[1]
    return line


@collector("shell", "命令习惯", "habits")
def collect() -> list[Signal]:
    signals: list[Signal] = []

    shell = os.environ.get("SHELL")
    if shell:
        signals.append(Signal("Shell", shell.rsplit("/", 1)[-1], shell))

    hist_name = _history_file()
    if hist_name:
        raw = read_text(home() / hist_name, max_bytes=4_000_000) or ""
        cmd_counter: dict[str, int] = {}
        tool_counter: dict[str, int] = {}
        total = 0
        for line in raw.splitlines():
            cmd = _parse_zsh_line(line).strip()
            if not cmd:
                continue
            total += 1
            head = cmd.split()[0] if cmd.split() else ""
            head = head.rsplit("/", 1)[-1]
            if head and head not in _BORING and not head.startswith("-"):
                tool_counter[head] = tool_counter.get(head, 0) + 1
            # For multi-word tools (git push, docker compose) capture two tokens.
            toks = cmd.split()
            if len(toks) >= 2 and toks[0] in {"git", "docker", "kubectl", "npm", "uv", "cargo"}:
                key = f"{toks[0]} {toks[1]}"
                cmd_counter[key] = cmd_counter.get(key, 0) + 1

        if total:
            signals.append(Signal("历史命令数", total, hist_name))
        if tool_counter:
            signals.append(Signal("最常用工具", top_counts(tool_counter, 15)))
        if cmd_counter:
            signals.append(Signal("最常用子命令", top_counts(cmd_counter, 12)))

    # rc file: aliases + oh-my-zsh theme/plugins + notable exports.
    for rc in (".zshrc", ".bashrc", ".bash_profile", ".profile"):
        text = read_text(home() / rc, max_bytes=400_000)
        if not text:
            continue
        aliases = re.findall(r"^\s*alias\s+([\w.-]+)=", text, re.MULTILINE)
        if aliases:
            signals.append(
                Signal(f"{rc} 别名", list(dict.fromkeys(aliases))[:20], f"{len(aliases)} 个")
            )
        theme = re.search(r'ZSH_THEME=["\']?([\w/-]+)', text)
        if theme:
            signals.append(Signal("zsh 主题", theme.group(1)))
        plugins = re.search(r"plugins=\(([^)]*)\)", text)
        if plugins:
            names = [p for p in plugins.group(1).split() if p]
            if names:
                signals.append(Signal("zsh 插件", names[:20]))
        break  # only the first present rc that yields anything

    return signals
