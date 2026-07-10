"Tests for test pr7 retirement grep."

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "persome"
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

#: Python identifiers that must no longer appear as code (NAME tokens).
FORBIDDEN_NAMES = {
    "entry_chain",
    "chain_get_chain_id",
    "chain_insert_new",
    "chain_link_supersede",
    "chain_retire",
    "recall_use_chain_index",
    "recall_read_evo_nodes",
    "dual_read_check_enabled",
    "dual_read",
    "cutover",
}

#: Retired tables — kept in old DBs (no DROP), but no live SQL may touch them.
_SQL_RE = re.compile(
    r"\b(?:FROM|INTO|TABLE|UPDATE|JOIN)\s+"
    r"(?:entry_chain|dual_read_runs|dual_read_diffs|cutover_drills)\b",
    re.IGNORECASE,
)

#: Retired CLI command names — must not be referenced from any string literal.
FORBIDDEN_COMMANDS = (
    "evomem-dual-read-check",
    "evomem-cutover-status",
    "evomem-record-drill",
)

_STRING_TOKEN_TYPES = {tokenize.STRING} | (
    {tokenize.FSTRING_MIDDLE} if hasattr(tokenize, "FSTRING_MIDDLE") else set()
)


def _scan(path: Path) -> list[str]:
    findings: list[str] = []
    text = path.read_text(encoding="utf-8")
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type == tokenize.NAME and tok.string in FORBIDDEN_NAMES:
            findings.append(f"{path}:{tok.start[0]}: identifier `{tok.string}`")
        elif tok.type in _STRING_TOKEN_TYPES:
            for m in _SQL_RE.finditer(tok.string):
                findings.append(f"{path}:{tok.start[0]}: SQL reference `{m.group(0)}`")
            for cmd in FORBIDDEN_COMMANDS:
                if cmd in tok.string:
                    findings.append(f"{path}:{tok.start[0]}: retired CLI command `{cmd}`")
    return findings


def test_retired_objects_have_no_readers() -> None:
    findings: list[str] = []
    for base in (SRC, SCRIPTS):
        for path in sorted(base.rglob("*.py")):
            findings.extend(_scan(path))
    assert not findings, "PR-7 retired objects still have readers:\n" + "\n".join(findings)
