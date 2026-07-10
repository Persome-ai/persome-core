"""Pydantic input models for writer tools (F7 — validation before dispatch)."""

from __future__ import annotations

from pydantic import BaseModel, Field


# Meta-cognition fields (Hy-Memory migration) are intentionally typed loose
# (``str | None``) rather than a Literal: an off-vocabulary confidence level from
# the LLM should degrade to "no tag" in the writer, not hard-fail validation.
class AppendInput(BaseModel):
    path: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1, max_length=50_000)
    tags: list[str] = []
    metadata: dict = {}
    confidence: str | None = Field(default=None, max_length=16)
    conflicted: bool = False
    occurred_at: str | None = Field(
        default=None,
        max_length=40,
        description=(
            "ISO-8601 time with NO spaces — separate date and time with 'T' "
            "(e.g. 2026-06-09T14:30). A space-separated value would be truncated "
            "when written as a heading tag."
        ),
    )


class CreateInput(BaseModel):
    path: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=500)
    tags: list[str] = []
    metadata: dict = {}


class SupersedeInput(BaseModel):
    path: str = Field(min_length=1, max_length=256)
    old_entry_id: str = Field(min_length=1)
    new_content: str = Field(min_length=1, max_length=50_000)
    reason: str = Field(default="", max_length=500)
    tags: list[str] = []
    confidence: str | None = Field(default=None, max_length=16)
    conflicted: bool = False
    occurred_at: str | None = Field(
        default=None,
        max_length=40,
        description=(
            "ISO-8601 time with NO spaces — separate date and time with 'T' "
            "(e.g. 2026-06-09T14:30). A space-separated value would be truncated "
            "when written as a heading tag."
        ),
    )


class FlagCompactInput(BaseModel):
    path: str = Field(min_length=1, max_length=256)
    reason: str = Field(default="", max_length=500)


class ReadMemoryInput(BaseModel):
    path: str = Field(min_length=1, max_length=256)
    tail_n: int = Field(default=10, ge=0, le=200)


class SearchMemoryInput(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=50)
    include_superseded: bool = False
    path_prefix: str | None = Field(default=None, max_length=64)


class DrillCaptureInput(BaseModel):
    capture_id: str = Field(min_length=1)
    text_limit: int = Field(default=2000, ge=100, le=8000)


class DrillChatCapturesInput(BaseModel):
    app_name: str = Field(min_length=1, max_length=200)
    start_ts: str = Field(min_length=1, max_length=50)
    end_ts: str = Field(min_length=1, max_length=50)
    max_bytes: int = Field(default=12_000, ge=1000, le=50_000)


class CommitInput(BaseModel):
    summary: str = Field(default="", max_length=500)


TOOL_INPUT_MODELS: dict[str, type[BaseModel]] = {
    "append": AppendInput,
    "create": CreateInput,
    "supersede": SupersedeInput,
    "flag_compact": FlagCompactInput,
    "read_memory": ReadMemoryInput,
    "search_memory": SearchMemoryInput,
    "drill_capture": DrillCaptureInput,
    "drill_chat_captures": DrillChatCapturesInput,
    "commit": CommitInput,
}
