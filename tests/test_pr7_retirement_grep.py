"""PR-7 退役对象「已无读者」grep 断言（SSOT 切换设计 §7 P0 守护）.

evomem retirement requires a grep assertion that deleted objects have no
readers. 本测试把它定型为持续闸门：扫描 ``src/persome``（生产代码）与 ``scripts``（运维探针）
的每个 ``.py``，断言任何退役对象都没有**代码级**读者——

- **标识符级**（tokenize NAME token）：entry_chain 三/四 helper
  （``chain_get_chain_id`` / ``chain_insert_new`` / ``chain_link_supersede``
  / ``chain_retire``）、退役 staging flag（``recall_use_chain_index`` /
  ``recall_read_evo_nodes`` / ``dual_read_check_enabled``）、退役模块名
  （``dual_read`` / ``cutover``）以及 ``entry_chain`` 本身。
- **SQL 级**（字符串字面量内 ``FROM/INTO/TABLE/UPDATE/JOIN <retired-table>``）：
  ``entry_chain`` / ``dual_read_runs`` / ``dual_read_diffs`` /
  ``cutover_drills``——老库里这些表原样保留不 DROP（留表不读），但任何新
  SQL 引用都是回归。
- **CLI 级**：退役命令名（``evomem-dual-read-check`` /
  ``evomem-cutover-status`` / ``evomem-record-drill``）不得再出现在任何
  字符串字面量（防止 help/错误提示把用户指向已删除的命令）。

注释与 docstring 里的**历史叙述**（"entry_chain 已退役"之类）是合法的考古
记号，故标识符扫描走 tokenize 的 NAME token（注释/字符串天然不命中），
字符串扫描只锚 SQL 形态与完整命令名。
"""

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
    """设计稿 §7：entry_chain/三 helper/dual_read/cutover/退役 flag 全库无代码级读者。"""
    findings: list[str] = []
    for base in (SRC, SCRIPTS):
        for path in sorted(base.rglob("*.py")):
            findings.extend(_scan(path))
    assert not findings, "PR-7 retired objects still have readers:\n" + "\n".join(findings)
