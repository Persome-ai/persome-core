"""Pydantic models for the Chat Session API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ─── Session ───────────────────────────────────────────────────────────────


class ChatMessageBlock(BaseModel):
    """One block in an assistant/user turn — preserves the Anthropic content-list
    structure so clients can render text + tool calls in their original order.

    Each block carries the subset of fields relevant to its ``type``; other
    fields are ``None`` (omitted from the wire form because of
    ``exclude_none``-style consumers).
    """

    type: Literal["text", "tool_use", "tool_result", "thinking"] = Field(
        description="Content block type"
    )
    text: str | None = Field(default=None, description="Text content when type=text")
    thinking: str | None = Field(
        default=None,
        description=(
            "Extended-thinking content when type=thinking. The Anthropic SDK persists "
            "this field, and replay must retain it for signature validation."
        ),
    )
    name: str | None = Field(
        default=None,
        description="Tool name when type=tool_use or type=tool_result",
    )
    input: dict[str, Any] | None = Field(
        default=None,
        description="Tool arguments when type=tool_use",
    )
    content: str | None = Field(
        default=None,
        description="Daemon-serialized tool result when type=tool_result",
    )
    tool_use_id: str | None = Field(
        default=None,
        description="ID of the associated tool_use block when type=tool_result",
    )


class ChatMessage(BaseModel):
    role: str = Field(description="Message role: system, user, or assistant")
    content: str | None = Field(
        default=None,
        description=(
            "Plain-text projection formed by concatenating text blocks. Structured tool "
            "call data is available in ``blocks``."
        ),
    )
    blocks: list[ChatMessageBlock] | None = Field(
        default=None,
        description=(
            "Content blocks in chronological order: text, thinking, tool_use, and "
            "tool_result. Capable clients can preserve interleaved text and tool events; "
            "other clients may read the plain-text ``content`` projection."
        ),
    )


class ChatSessionInfo(BaseModel):
    id: str = Field(description="Eight-character session UUID")
    created_at: str = Field(description="Creation time in ISO 8601 format")
    updated_at: str = Field(description="Last update time in ISO 8601 format")
    turn_count: int = Field(description="Conversation turn count, measured by user messages")
    archived: bool = Field(default=False, description="Whether the session is archived")
    title: str | None = Field(
        default=None,
        description=(
            "Short LLM-generated title produced after the first assistant response. "
            "Clients should prefer it in sidebars; it is null when unavailable."
        ),
    )
    preview: str | None = Field(
        default=None,
        description=(
            "Truncated preview of the first user message for sidebar fallback. It is null "
            "for an empty session and has lower priority than ``title``."
        ),
    )


class CreateSessionResponse(BaseModel):
    session: ChatSessionInfo = Field(description="Session metadata")


# ─── Messages ──────────────────────────────────────────────────────────────


class SendMessageRequest(BaseModel):
    content: str = Field(..., description="User message content")


class ChatSessionDetail(BaseModel):
    session: ChatSessionInfo = Field(description="Session metadata")
    messages: list[ChatMessage] = Field(description="Messages")


# ─── SSE events (POST /chat/sessions/{id}/messages) ────────────────────────
#
# Wire format: each SSE frame is ``data: <json>\n\n``. Streams end with
# ``data: [DONE]\n\n``. Reply tokens arrive incrementally; tool_call /
# tool_result frames bracket each tool invocation; an ``error`` frame may
# replace the ``done`` frame on failure.


class SSEReplyEvent(BaseModel):
    type: Literal["reply"] = Field(description="Event type constant")
    content: str = Field(description="Incremental token text")


class SSEReasoningEvent(BaseModel):
    type: Literal["reasoning"] = Field(description="Event type constant")
    content: str = Field(description="Incremental extended-thinking text")


class SSEToolCallEvent(BaseModel):
    type: Literal["tool_call"] = Field(description="Event type constant")
    name: str = Field(description="Tool name")
    arguments: dict = Field(description="Tool arguments")


class SSEToolResultEvent(BaseModel):
    type: Literal["tool_result"] = Field(description="Event type constant")
    name: str = Field(description="Tool name")
    content: str = Field(description="Tool result text")


class SSEErrorEvent(BaseModel):
    type: Literal["error"] = Field(description="Event type constant")
    message: str = Field(description="Error description")


class SSEDoneEvent(BaseModel):
    type: Literal["done"] = Field(description="Event type constant for a completed SSE turn")
    ttft_ms: float | None = Field(
        default=None,
        description="Time to first token in milliseconds; null when no token was emitted",
    )


SendMessageEvent = (
    SSEReplyEvent
    | SSEReasoningEvent
    | SSEToolCallEvent
    | SSEToolResultEvent
    | SSEErrorEvent
    | SSEDoneEvent
)
"""Discriminated union of the SSE events emitted by send_message."""
