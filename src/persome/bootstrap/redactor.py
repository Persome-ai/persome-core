"""Condense collector output into the single summary that leaves the machine.

This is the privacy choke point. Everything the LLM sees comes from here, and
nothing else does. The redactor:

- includes only collectors that produced signals,
- renders aggregates compactly (``name (count)`` / ``name — detail``),
- enforces per-list and total-size caps so a chatty machine can't balloon the
  prompt or smuggle raw data through.

Output is plain Markdown text — easy for the LLM to read and easy for a human
to eyeball if they want to see exactly what was sent.
"""

from __future__ import annotations

from typing import Any

from .collectors import CollectorResult

_MAX_LIST = 20
_MAX_TOTAL_CHARS = 12_000


def _render_value(value: Any) -> str:
    if isinstance(value, list):
        parts: list[str] = []
        for item in value[:_MAX_LIST]:
            if isinstance(item, dict):
                name = str(item.get("name", "")).strip()
                count = item.get("count")
                detail = str(item.get("detail", "")).strip()
                piece = name
                if count:
                    piece += f" ({count})"
                if detail:
                    piece += f" — {detail}"
                parts.append(piece)
            else:
                parts.append(str(item))
        return ", ".join(p for p in parts if p)
    return str(value)


def build(results: list[CollectorResult]) -> str:
    """Render produced collector results into a capped Markdown summary."""
    lines: list[str] = [
        "以下是从这台机器本地榨取的、关于使用者的信号(均为聚合/元数据,无原始内容)。",
        "",
    ]
    for r in results:
        if not r.produced:
            continue
        lines.append(f"## {r.title}")
        for s in r.signals:
            rendered = _render_value(s.value)
            suffix = f"  ({s.detail})" if s.detail else ""
            lines.append(f"- {s.label}: {rendered}{suffix}")
        lines.append("")

    text = "\n".join(lines).strip()
    if len(text) > _MAX_TOTAL_CHARS:
        text = text[:_MAX_TOTAL_CHARS] + "\n\n…(已截断)"
    return text
