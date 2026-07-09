"""System2 Schema Miner —— 从关联事实归纳预测性心智模型（teardown §6）。

对齐 Hy-Memory 的 ``Abstractor.abstract_schema``：产出
``{central_proposition, supporting_summary, expected_inferences[], confidence}``，
写入 L6_SCHEMA。LLM 走依赖注入的 ``llm_call``（OpenAI 形返回），测试注入 fake。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._json import parse_json_object

LLMCall = Callable[[list[dict]], Any]

_PROMPT_PATH = Path(__file__).parent / "prompts" / "schema_miner.md"


def _load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _content_of(resp: Any) -> str:
    try:
        return resp.choices[0].message.content or ""
    except (AttributeError, IndexError):
        return ""


@dataclass
class SchemaResult:
    success: bool
    central_proposition: str = ""
    supporting_summary: str = ""
    expected_inferences: list[str] = field(default_factory=list)
    confidence: float = 0.0
    error: str | None = None


class SchemaMiner:
    def __init__(self, llm_call: LLMCall, prompt: str | None = None) -> None:
        self._llm_call = llm_call
        self._prompt = prompt if prompt is not None else _load_prompt()

    def mine_schema(self, facts: list[str]) -> SchemaResult:
        """从一组关联事实归纳 schema；解析失败时 ``success=False``。"""
        messages = self._build_messages(facts)
        parsed = parse_json_object(_content_of(self._llm_call(messages)))
        if parsed is None:
            return SchemaResult(success=False, error="无法解析 schema JSON")
        inferences = parsed.get("expected_inferences") or []
        if not isinstance(inferences, list):
            inferences = []
        return SchemaResult(
            success=True,
            central_proposition=str(parsed.get("central_proposition", "")),
            supporting_summary=str(parsed.get("supporting_summary", "")),
            expected_inferences=[str(x) for x in inferences],
            confidence=_as_float(parsed.get("confidence")),
        )

    def _build_messages(self, facts: list[str]) -> list[dict]:
        fact_lines = "\n".join(f"- {f}" for f in facts) or "（无事实）"
        user = f"## 关联事实\n{fact_lines}\n\n请按系统提示归纳 schema 并输出 JSON。"
        return [
            {"role": "system", "content": self._prompt},
            {"role": "user", "content": user},
        ]


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
