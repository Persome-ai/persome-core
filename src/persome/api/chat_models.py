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

    type: Literal["text", "tool_use", "tool_result", "thinking"] = Field(description="块类型")
    text: str | None = Field(default=None, description="文本块的文本内容（type=text）")
    thinking: str | None = Field(
        default=None,
        description=(
            "模型 extended thinking 的文本内容（type=thinking）。"
            "Anthropic SDK 持久化该字段，重放对话时必须保留以满足签名校验。"
        ),
    )
    name: str | None = Field(
        default=None,
        description="工具名（type=tool_use / tool_result）",
    )
    input: dict[str, Any] | None = Field(
        default=None,
        description="工具调用入参（type=tool_use）",
    )
    content: str | None = Field(
        default=None,
        description="工具调用结果字符串（type=tool_result，daemon 已序列化好）",
    )
    tool_use_id: str | None = Field(
        default=None,
        description="关联 tool_use 块的 id（type=tool_result）",
    )


class ChatMessage(BaseModel):
    role: str = Field(description="消息角色：system/user/assistant")
    content: str | None = Field(
        default=None,
        description=(
            "消息内容的纯文本投影 —— 所有 text 块拼接而成。Tool 调用结构化信息见 ``blocks``。"
        ),
    )
    blocks: list[ChatMessageBlock] | None = Field(
        default=None,
        description=(
            "按时序排列的内容块（text / thinking / tool_use / tool_result）。"
            "支持的客户端用它复刻 agent 一轮里 text↔tool 的交错时序；"
            "不支持的客户端仍可以只读 ``content`` 拿到纯文本投影。"
        ),
    )


class ChatSessionInfo(BaseModel):
    id: str = Field(description="会话 ID（8 位短 UUID）")
    created_at: str = Field(description="创建时间 ISO8601")
    updated_at: str = Field(description="更新时间 ISO8601")
    turn_count: int = Field(description="对话轮次（user 消息数）")
    archived: bool = Field(default=False, description="是否已归档")
    title: str | None = Field(
        default=None,
        description=(
            "LLM 生成的短标题（≤24 字），首轮 assistant 回复后异步生成并落盘。"
            "客户端侧边栏应优先使用此字段；未生成或失败时为 null。"
        ),
    )
    preview: str | None = Field(
        default=None,
        description=(
            "首条 user 消息截断后的预览（≤80 字），用于侧边栏 fallback；"
            "空会话为 null。优先级低于 ``title``。"
        ),
    )


class CreateSessionResponse(BaseModel):
    session: ChatSessionInfo = Field(description="会话信息")


# ─── Messages ──────────────────────────────────────────────────────────────


class SendMessageRequest(BaseModel):
    content: str = Field(..., description="用户消息内容")


class ToolCallExecuted(BaseModel):
    name: str = Field(description="工具调用名称")
    arguments: dict = Field(description="工具调用参数")


class SendMessageResponse(BaseModel):
    message: ChatMessage = Field(description="assistant 回复消息")
    tool_calls_executed: list[ToolCallExecuted] | None = Field(
        default=None, description="执行的工具调用列表"
    )
    usage: dict[str, int] | None = Field(
        default=None, description="token 使用量，如 {'input_tokens': 100, 'output_tokens': 50}"
    )
    did_compress: bool = Field(default=False, description="是否进行了对话历史压缩")
    did_microcompact: bool = Field(default=False, description="是否进行了微压缩")
    reasoning: str | None = Field(default=None, description="推理内容（如果模型支持 reasoning）")
    error: str | None = Field(default=None, description="错误信息（如果本轮出现错误）")


class ChatSessionDetail(BaseModel):
    session: ChatSessionInfo = Field(description="会话信息")
    messages: list[ChatMessage] = Field(description="消息列表")


# ─── SSE events (POST /chat/sessions/{id}/messages) ────────────────────────
#
# Wire format: each SSE frame is ``data: <json>\n\n``. Streams end with
# ``data: [DONE]\n\n``. Reply tokens arrive incrementally; tool_call /
# tool_result frames bracket each tool invocation; an ``error`` frame may
# replace the ``done`` frame on failure.


class SSEReplyEvent(BaseModel):
    type: Literal["reply"] = Field(description="事件类型常量")
    content: str = Field(description="增量 token 文本片段")


class SSEReasoningEvent(BaseModel):
    type: Literal["reasoning"] = Field(description="事件类型常量")
    content: str = Field(description="增量 thinking / 推理片段（extended thinking）")


class SSEToolCallEvent(BaseModel):
    type: Literal["tool_call"] = Field(description="事件类型常量")
    name: str = Field(description="工具调用名称")
    arguments: dict = Field(description="工具调用参数")


class SSEToolResultEvent(BaseModel):
    type: Literal["tool_result"] = Field(description="事件类型常量")
    name: str = Field(description="工具调用名称")
    content: str = Field(description="工具调用结果字符串")


class SSEErrorEvent(BaseModel):
    type: Literal["error"] = Field(description="事件类型常量")
    message: str = Field(description="错误描述")


class SSEDoneEvent(BaseModel):
    type: Literal["done"] = Field(description="事件类型常量；表示本轮 SSE 正常完成")
    ttft_ms: float | None = Field(
        default=None,
        description="本轮首 token 到达耗时（毫秒，time-to-first-token）；无 token 流出时为 null",
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
