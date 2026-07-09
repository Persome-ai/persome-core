"""R4/#544 退役对象「已无读者」grep 断言（timeline helpful_intent_tags 旧层）.

仿 PR-7 entry_chain 退役的同款持续闸门（``test_pr7_retirement_grep.py``）：
扫描 ``src/persome``（生产代码）与 ``scripts``（运维探针）的每个
``.py``，断言旧层没有**代码级**读者——

- **标识符级**（tokenize NAME token）：旧层验证器及其常量
  （``_validate_intent_tag`` / ``_VALID_INTENT_KINDS`` /
  ``_INTENT_CONFIDENCE_FLOOR``）不得在任何文件出现；
  ``helpful_intent_tags`` 本身只允许出现在保列白名单——
  ``timeline/store.py``（schema/serde，列保留给历史行）与
  ``api/routes.py``（legacy-only 展示）。
- **prompt 级**：``src/persome/prompts/*.md`` 不得再出现
  ``helpful_intent_tags``——prompt 重新要求该字段即旧层复活。

注释与 docstring 里的**历史叙述**（"helpful_intent_tags 已退役"之类）是
合法的考古记号：标识符扫描走 tokenize 的 NAME token，注释/字符串天然
不命中。
"""

from __future__ import annotations

import io
import tokenize
from pathlib import Path

_PKG = Path(__file__).resolve().parents[1]
SRC = _PKG / "src" / "persome"
SCRIPTS = _PKG / "scripts"
PROMPTS = SRC / "prompts"

#: Old-layer identifiers that must no longer appear as code (NAME tokens).
FORBIDDEN_NAMES = {
    "_validate_intent_tag",
    "_VALID_INTENT_KINDS",
    "_INTENT_CONFIDENCE_FLOOR",
}

#: ``helpful_intent_tags`` survives ONLY here (保列停写: schema/serde for
#: historical rows + the legacy-only API display field).
_COLUMN_ALLOWLIST = {
    SRC / "timeline" / "store.py",
    SRC / "api" / "routes.py",
}


def _scan(path: Path) -> list[str]:
    findings: list[str] = []
    text = path.read_text(encoding="utf-8")
    column_allowed = path in _COLUMN_ALLOWLIST
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type != tokenize.NAME:
            continue
        if tok.string in FORBIDDEN_NAMES:
            findings.append(f"{path}:{tok.start[0]}: identifier `{tok.string}`")
        elif tok.string == "helpful_intent_tags" and not column_allowed:
            findings.append(f"{path}:{tok.start[0]}: identifier `helpful_intent_tags`")
    return findings


def test_retired_intent_tag_layer_has_no_readers() -> None:
    """#544：旧层验证器全库无读者；列名只剩保列白名单两处。"""
    findings: list[str] = []
    for base in (SRC, SCRIPTS):
        for path in sorted(base.rglob("*.py")):
            findings.extend(_scan(path))
    assert not findings, "R4 retired old layer still has readers:\n" + "\n".join(findings)


def test_prompts_do_not_request_intent_tags() -> None:
    """timeline prompt 重新要求 ``helpful_intent_tags`` 字段 = 旧层复活，必须挡住。"""
    offenders = [
        str(p) for p in sorted(PROMPTS.glob("*.md")) if "helpful_intent_tags" in p.read_text()
    ]
    assert not offenders, "prompts still request helpful_intent_tags:\n" + "\n".join(offenders)
