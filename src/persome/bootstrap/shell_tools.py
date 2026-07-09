"""Raw shell tool — full exploration freedom, the way Claude Code explores a repo.

The cold-start agent gets a real shell so it can investigate the machine however
it sees fit (`find`, `mdfind`, `cat`, `ls -R`, `osascript` to query Photos /
Calendar, …) instead of being limited to a handful of curated readers. This is
deliberate and user-authorized: it runs on the user's own machine for their own
profile.

The only limits are *operational*, not privacy guardrails — a timeout so a
command can't hang the run, and an output cap so a chatty command can't blow the
context window. (Claude Code's own Bash tool has the same two limits.)
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..logger import get

logger = get("persome.bootstrap")

ToolHandler = Callable[[dict[str, Any]], Any]

_TIMEOUT_S = 20.0
_MAX_OUTPUT = 16_000


def _shell_argv(cmd: str) -> list[str]:
    """Login zsh on macOS; fall back to bash/sh where zsh is absent (Linux CI)."""
    for shell in ("/bin/zsh", "/bin/bash"):
        if Path(shell).exists():
            return [shell, "-lc", cmd]
    return ["/bin/sh", "-c", cmd]


def run_shell(command: str, *, timeout: float = _TIMEOUT_S) -> dict[str, Any]:
    """Run ``command`` in a login zsh from the home dir; return output + exit code."""
    cmd = (command or "").strip()
    if not cmd:
        return {"error": "empty command"}
    try:
        proc = subprocess.run(  # noqa: S602 — intentional shell access for exploration
            _shell_argv(cmd),
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(Path.home()),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"command": cmd, "error": f"timed out after {timeout:.0f}s"}
    except OSError as exc:
        return {"command": cmd, "error": f"{type(exc).__name__}: {exc}"}

    out = proc.stdout or ""
    err = proc.stderr or ""
    combined = out if not err else f"{out}\n[stderr]\n{err}"
    truncated = len(combined) > _MAX_OUTPUT
    if truncated:
        combined = combined[:_MAX_OUTPUT] + "\n…(output truncated)"
    result: dict[str, Any] = {"command": cmd, "exit_code": proc.returncode, "output": combined}
    if truncated:
        result["note"] = f"output truncated to {_MAX_OUTPUT} chars"
    return result


def build_shell_tools() -> tuple[list[dict[str, Any]], dict[str, ToolHandler]]:
    """Return (schemas, handlers) for the raw `run_shell` tool."""
    schema = {
        "name": "run_shell",
        "description": (
            "Run one shell command in the user's home dir (login zsh); returns stdout/stderr "
            "and exit code. You have full shell freedom — explore the machine like a person at "
            "a terminal: use ls/find/mdfind/cat/head/grep/osascript or anything to inspect "
            "documents, photos, calendar, browser, apps, configs, etc. 20s timeout, output "
            "capped at 16KB."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "the shell command to run"}
            },
            "required": ["command"],
        },
    }

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        return run_shell(str(args.get("command", "")))

    return [schema], {"run_shell": handler}
