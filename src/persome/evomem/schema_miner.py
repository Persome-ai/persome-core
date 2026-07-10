"LLM adapter for predictive schema mining."

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
        """Infer a schema from related facts and fail cleanly on invalid output."""
        messages = self._build_messages(facts)
        parsed = parse_json_object(_content_of(self._llm_call(messages)))
        if parsed is None:
            return SchemaResult(success=False, error="could not parse schema JSON")
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
        fact_lines = "\n".join(f"- {f}" for f in facts) or "(no facts)"
        user = (
            f"## Related facts\n{fact_lines}\n\n"
            "Infer a schema and return the JSON object defined by the system prompt."
        )
        return [
            {"role": "system", "content": self._prompt},
            {"role": "user", "content": user},
        ]


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
