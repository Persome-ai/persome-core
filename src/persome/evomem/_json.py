"Fault-tolerant JSON extraction helpers for model responses."

from __future__ import annotations

import json
import re
from typing import Any

_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _strip_fences(text: str) -> str:
    m = _FENCE_RE.search(text)
    return m.group(1).strip() if m else text.strip()


def parse_json_object(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    stripped = _strip_fences(text)
    candidates: list[str] = [text, stripped]
    m = _OBJECT_RE.search(stripped)
    if m:
        candidates.append(m.group(0))
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None
