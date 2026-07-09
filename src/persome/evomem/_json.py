"""共享 JSON 解析工具：从 LLM 文本里剥 ```json 围栏 + 正则兜底取首个对象。

reconciler / schema_miner 共用（DRY）。模型经常把 JSON 夹在解说文字里或包进
代码围栏，所以解析要稳健：先试整串，再试去围栏后的内容，最后正则抓第一个
``{...}`` 跨度。永不抛异常 —— 解析失败返回 ``None``，由调用方决定降级行为。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 第一个平衡度未知但贪婪到末尾的对象跨度；配合 json.loads 的容错足够 MVP 用。
_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _strip_fences(text: str) -> str:
    """若文本含 ```json ... ``` 围栏，返回围栏内内容；否则原样返回。"""
    m = _FENCE_RE.search(text)
    return m.group(1).strip() if m else text.strip()


def parse_json_object(text: str | None) -> dict[str, Any] | None:
    """把 ``text`` 解析成一个 JSON 对象（dict）；任何失败返回 ``None``。

    依次尝试：原串 → 去围栏 → 正则抓首个 ``{...}`` 跨度。只接受 dict 结果
    （顶层数组 / 标量视为失败），调用方据此判断是否降级。
    """
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
