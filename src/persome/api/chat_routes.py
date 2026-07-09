"""FastAPI routes for the Chat Session API."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import threading
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException
from fastapi import Path as FastPath
from pydantic import TypeAdapter
from sse_starlette.sse import EventSourceResponse

from .. import paths
from ..chat.handler import _load_system_prompt, _run_turn
from ..chat.memory_extractor import maybe_extract as maybe_extract_memories
from ..config import Config
from ..config import load as load_config
from ..logger import get
from ..writer.chat_title import generate_title as _generate_chat_title
from .chat_models import (
    ChatMessage,
    ChatMessageBlock,
    ChatSessionDetail,
    ChatSessionInfo,
    CreateSessionResponse,
    SendMessageEvent,
    SendMessageRequest,
)
from .models import ApiResponse

# JSON schema for the SSE event union, inlined into the OpenAPI spec so that
# clients can typecheck individual frames. Computed once at import time.
_SEND_MESSAGE_EVENT_SCHEMA: dict[str, Any] = TypeAdapter(SendMessageEvent).json_schema()

logger = get("persome.api.chat")

_cfg: Config | None = None


def set_config(cfg: Config | None) -> None:
    global _cfg
    _cfg = cfg


def _get_cfg() -> Config:
    return _cfg or load_config()


def _history_dir() -> Path:
    d = paths.root() / "chat-history"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _session_path(session_id: str) -> Path:
    return _history_dir() / f"api-{session_id}.json"


@dataclass
class ChatSession:
    id: str
    created_at: str
    updated_at: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    last_usage: dict[str, int] | None = None
    consecutive_compact_failures: int = 0
    last_extracted_index: int = 0
    tokens_at_last_extraction: int = 0
    last_assistant_time: float | None = None
    archived: bool = False
    # LLM-generated short title (≤TITLE_MAX_CHARS) for sidebar display.
    # None until the first assistant reply has been produced; once written,
    # never auto-regenerated. Persists in the session JSON.
    title: str | None = None


_sessions: dict[str, ChatSession] = {}
_sessions_lock = threading.Lock()


def _save_session(session: ChatSession) -> None:
    """Persist non-system messages to disk."""
    saveable = [m for m in session.messages if m.get("role") != "system"]
    data = {
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "messages": saveable,
        "title": session.title,
    }
    _session_path(session.id).write_text(
        json.dumps(data, ensure_ascii=False, default=str, indent=2)
    )


def _load_session(session_id: str) -> ChatSession | None:
    """Load a session from disk and rehydrate the system prompt."""
    p = _session_path(session_id)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None

    created_at = data.get("created_at", datetime.now().astimezone().isoformat())
    system_content = _load_system_prompt().format(current_time=created_at)
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_content}]
    loaded = data.get("messages", [])
    if isinstance(loaded, list):
        messages.extend(loaded)

    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        title = None

    return ChatSession(
        id=session_id,
        created_at=created_at,
        updated_at=data.get("updated_at", created_at),
        messages=messages,
        title=title,
    )


def _turn_count(session: ChatSession) -> int:
    return len([m for m in session.messages if m.get("role") == "user"])


_PREVIEW_MAX_CHARS = 80

_CURRENT_TIME_PREFIX_RE = re.compile(r"^\[Current time: [^\]]+\]\n\n")


def _strip_time_prefix(text: str) -> str:
    """Remove the ``[Current time: ...]`` prefix injected into user messages."""
    return _CURRENT_TIME_PREFIX_RE.sub("", text, count=1)


def _first_user_preview(messages: list[dict[str, Any]]) -> str | None:
    """Build a sidebar preview string from the first user turn.

    Walks ``messages`` in order, picks the first ``role=="user"`` entry,
    normalizes its content to a plain string (collapsing Anthropic SDK
    block lists via :func:`_content_to_text`), strips whitespace, and
    truncates with an ellipsis if it exceeds ``_PREVIEW_MAX_CHARS``.
    Returns ``None`` when there is no user message or the content is
    effectively empty — callers should treat ``None`` as "fall back to
    timestamp".
    """
    for m in messages:
        if m.get("role") != "user":
            continue
        text = _content_to_text(m.get("content"))
        if not text:
            continue
        text = _strip_time_prefix(text)
        cleaned = " ".join(text.split())
        if not cleaned:
            continue
        if len(cleaned) > _PREVIEW_MAX_CHARS:
            return cleaned[:_PREVIEW_MAX_CHARS].rstrip() + "…"
        return cleaned
    return None


def _content_to_text(content: Any) -> str | None:
    """Normalize a stored message ``content`` field to a plain string.

    Sessions persist assistant turns in the Anthropic SDK shape — content is
    a list of blocks like ``[{"type": "text", "text": "..."}, {"type":
    "tool_use", ...}]``. The HTTP contract advertises ``content: str | None``,
    so we collapse text blocks into one string here and drop non-text blocks
    (tool_use / thinking are surfaced via the SSE stream, not the history
    endpoint).
    """
    if content is None or isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        ]
        joined = "".join(parts)
        return joined or None
    return str(content)


def _is_real_user(content: Any) -> bool:
    """Distinguish actual user input from tool_result-carrying user messages.

    The agent loop appends synthetic ``role=user`` messages whose ``content``
    is a list containing ``tool_result`` blocks; those are internal to the
    loop and should not be surfaced as user-visible messages.
    """
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return False


def _projected_block(b: dict[str, Any]) -> dict[str, Any] | None:
    """Project a stored Anthropic content block to the wire-shape block.

    Stored blocks may carry extra fields (cache markers, signatures, other
    internal SDK keys); the wire shape only keeps what clients render.
    Unknown block types return ``None`` and are dropped.
    """
    btype = b.get("type")
    if btype == "text":
        text = b.get("text")
        if not isinstance(text, str) or not text:
            return None
        return {"type": "text", "text": text}
    if btype == "thinking":
        # Anthropic stores extended thinking blocks as
        # ``{"type": "thinking", "thinking": "...", "signature": "..."}``.
        # We drop ``signature`` (server-side validation only — clients
        # don't need it to render) and surface the prose.
        thinking = b.get("thinking")
        if not isinstance(thinking, str) or not thinking:
            return None
        return {"type": "thinking", "thinking": thinking}
    if btype == "tool_use":
        return {
            "type": "tool_use",
            "name": b.get("name") or "",
            "input": b.get("input") if isinstance(b.get("input"), dict) else {},
        }
    if btype == "tool_result":
        raw = b.get("content")
        # tool_result content in Anthropic SDK can itself be a list of blocks
        # (e.g. [{"type": "text", "text": "..."}]) or a plain string.
        if isinstance(raw, list):
            parts = [
                blk.get("text", "")
                for blk in raw
                if isinstance(blk, dict) and blk.get("type") == "text"
            ]
            stringified = "".join(parts)
        elif isinstance(raw, str):
            stringified = raw
        elif raw is None:
            stringified = ""
        else:
            stringified = str(raw)
        return {
            "type": "tool_result",
            "name": b.get("name"),  # daemon may or may not populate; tolerate None
            "content": stringified,
            "tool_use_id": b.get("tool_use_id"),
        }
    return None


def _projected_blocks(content: Any) -> list[dict[str, Any]]:
    """Project a stored ``content`` field (string or block list) to a list of
    wire-shape blocks. Plain strings become a single ``text`` block.
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        out: list[dict[str, Any]] = []
        for b in content:
            if isinstance(b, dict):
                projected = _projected_block(b)
                if projected is not None:
                    out.append(projected)
        return out
    return []


def _fold_agent_loop(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse Anthropic agent-loop iterations into one assistant turn.

    Storage keeps each loop iteration faithful (assistant text+tool_use →
    user tool_result → assistant text+tool_use → ... → final assistant
    text). The HTTP messages endpoint is a presentation projection that
    folds every span of non-real-user messages between two real-user
    messages into a single assistant entry. The folded entry carries:

      - ``blocks``: every text/tool_use/tool_result block from the span,
        in their original chronological order (so clients can render
        ``text → tool_call → tool_result → text`` exactly as it streamed).
      - ``content``: a string fallback — concatenation of all text blocks
        — for older clients that only read the ``content`` field.

    Caller passes ``raw`` with ``system`` messages already filtered out.
    """
    out: list[dict[str, Any]] = []
    pending_blocks: list[dict[str, Any]] = []

    def _flush_assistant() -> None:
        nonlocal pending_blocks
        if not pending_blocks:
            return
        text_parts = [b["text"] for b in pending_blocks if b["type"] == "text"]
        content_str = "".join(text_parts)
        out.append(
            {
                "role": "assistant",
                "content": content_str,
                "blocks": pending_blocks,
            }
        )
        pending_blocks = []

    for m in raw:
        role = m.get("role", "")
        content = m.get("content")
        if role == "user" and _is_real_user(content):
            _flush_assistant()
            text = _strip_time_prefix(_content_to_text(content) or "")
            out.append(
                {
                    "role": "user",
                    "content": text,
                    "blocks": [{"type": "text", "text": text}] if text else [],
                }
            )
        elif role == "user":
            # tool_result-carrying user message — pull tool_result blocks
            # into the in-flight assistant turn so they stay adjacent to
            # the tool_use that triggered them.
            pending_blocks.extend(_projected_blocks(content))
        elif role == "assistant":
            pending_blocks.extend(_projected_blocks(content))
    _flush_assistant()
    return out


router = APIRouter()


@router.post("/chat/sessions", response_model=ApiResponse, tags=["chat"])
def create_session() -> ApiResponse:
    """创建一个新的聊天会话，返回会话 ID 和初始信息。"""
    session_id = str(uuid.uuid4())[:8]
    now = datetime.now().astimezone().isoformat()
    system_content = _load_system_prompt().format(current_time=now)
    session = ChatSession(
        id=session_id,
        created_at=now,
        updated_at=now,
        messages=[{"role": "system", "content": system_content}],
    )
    _save_session(session)
    with _sessions_lock:
        _sessions[session_id] = session
    return ApiResponse(
        data=CreateSessionResponse(
            session=ChatSessionInfo(
                id=session_id,
                created_at=now,
                updated_at=now,
                turn_count=0,
                title=None,
                preview=None,
            )
        ).model_dump()
    )


@router.get("/chat/sessions", response_model=ApiResponse, tags=["chat"])
def list_sessions() -> ApiResponse:
    """列出所有聊天会话，包括活跃会话和已持久化到磁盘的会话。"""
    with _sessions_lock:
        active_ids = set(_sessions.keys())

    items: list[ChatSessionInfo] = []
    for p in sorted(_history_dir().glob("api-*.json")):
        sid = p.stem[4:]  # strip "api-" prefix
        if sid in active_ids:
            session = _sessions[sid]
            items.append(
                ChatSessionInfo(
                    id=sid,
                    created_at=session.created_at,
                    updated_at=session.updated_at,
                    turn_count=_turn_count(session),
                    title=session.title,
                    preview=_first_user_preview(session.messages),
                )
            )
        else:
            try:
                data = json.loads(p.read_text())
                disk_messages = data.get("messages", [])
                if not isinstance(disk_messages, list):
                    disk_messages = []
                disk_title = data.get("title")
                if not isinstance(disk_title, str) or not disk_title.strip():
                    disk_title = None
                items.append(
                    ChatSessionInfo(
                        id=sid,
                        created_at=data.get("created_at", ""),
                        updated_at=data.get("updated_at", ""),
                        turn_count=len([m for m in disk_messages if m.get("role") == "user"]),
                        title=disk_title,
                        preview=_first_user_preview(disk_messages),
                    )
                )
            except (OSError, json.JSONDecodeError):
                continue

    return ApiResponse(data={"count": len(items), "sessions": [i.model_dump() for i in items]})


@router.get("/chat/sessions/{session_id}", response_model=ApiResponse, tags=["chat"])
def get_session(
    session_id: Annotated[str, FastPath(description="会话 ID（8 位短 UUID）")],
) -> ApiResponse:
    """获取指定会话的详情，包括消息历史。"""
    with _sessions_lock:
        session = _sessions.get(session_id)

    if session is None:
        session = _load_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        with _sessions_lock:
            _sessions[session_id] = session

    return ApiResponse(
        data=ChatSessionDetail(
            session=ChatSessionInfo(
                id=session.id,
                created_at=session.created_at,
                updated_at=session.updated_at,
                turn_count=_turn_count(session),
                title=session.title,
                preview=_first_user_preview(session.messages),
            ),
            messages=[
                ChatMessage(role=m.get("role", ""), content=_content_to_text(m.get("content")))
                for m in session.messages
            ],
        ).model_dump()
    )


@router.get("/chat/sessions/{session_id}/messages", response_model=ApiResponse, tags=["chat"])
def get_session_messages(
    session_id: Annotated[str, FastPath(description="会话 ID（8 位短 UUID）")],
) -> ApiResponse:
    """获取指定会话的消息历史（不含 system 消息）。"""
    with _sessions_lock:
        session = _sessions.get(session_id)

    if session is None:
        session = _load_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        with _sessions_lock:
            _sessions[session_id] = session

    non_system = [m for m in session.messages if m.get("role") != "system"]
    folded = _fold_agent_loop(non_system)
    messages = [
        ChatMessage(
            role=m["role"],
            content=m["content"],
            blocks=[ChatMessageBlock(**b) for b in m["blocks"]] if m.get("blocks") else None,
        )
        for m in folded
    ]
    # Drop None fields per-block so the wire payload only carries fields
    # relevant to each block type (cleaner client parsing + smaller payload).
    return ApiResponse(data=[m.model_dump(exclude_none=True) for m in messages])


@router.post(
    "/chat/sessions/{session_id}/messages",
    tags=["chat"],
    response_class=EventSourceResponse,
    responses={
        200: {
            "description": (
                "Server-Sent Events 流。每帧为 ``data: <json>\\n\\n``，"
                "其中 ``<json>`` 形如 ``SendMessageEvent`` 联合体。"
                "流以 ``data: [DONE]\\n\\n`` 结束。"
            ),
            "content": {"text/event-stream": {"schema": _SEND_MESSAGE_EVENT_SCHEMA}},
        }
    },
)
async def send_message(
    session_id: Annotated[str, FastPath(description="会话 ID（8 位短 UUID）")],
    body: SendMessageRequest,
) -> EventSourceResponse:
    """向指定会话发送一条消息，以 SSE 流式回放 assistant token、工具调用。

    SSE 帧类型见 :data:`SendMessageEvent`：
    - ``reply``      增量 token（``content`` 字段）
    - ``tool_call``  工具调用开始（``name`` + ``arguments``）
    - ``tool_result``工具调用结果（``name`` + ``content``）
    - ``error``      本轮失败（``message`` 字段）
    - ``done``       本轮正常结束

    Session 持久化在 stream 结束时执行。客户端断开时会取消正在跑的 LLM 任务。
    """
    cfg = _get_cfg()
    with _sessions_lock:
        session = _sessions.get(session_id)

    if session is None:
        session = _load_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        with _sessions_lock:
            _sessions[session_id] = session

    # Bound the queue so a slow client can't let LLM tokens balloon in memory.
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=256)

    async def _on_token(tok: str) -> None:
        if tok:
            await queue.put({"type": "reply", "content": tok})

    async def _on_thinking(chunk: str) -> None:
        if chunk:
            await queue.put({"type": "reasoning", "content": chunk})

    async def _on_tool_call(
        name: str, args: dict[str, Any], result_str: str, _elapsed_ms: float
    ) -> None:
        await queue.put({"type": "tool_call", "name": name, "arguments": args})
        await queue.put({"type": "tool_result", "name": name, "content": result_str})

    async def _run_and_persist() -> None:
        try:
            turn_result = await _run_turn(
                cfg,
                session.messages,
                body.content,
                last_usage=session.last_usage,
                consecutive_compact_failures=session.consecutive_compact_failures,
                last_assistant_time=session.last_assistant_time,
                on_token=_on_token,
                on_thinking=_on_thinking,
                on_tool_call=_on_tool_call,
            )

            session.messages = turn_result.messages
            session.last_usage = turn_result.usage or session.last_usage
            session.consecutive_compact_failures = turn_result.consecutive_compact_failures
            session.last_assistant_time = turn_result.last_assistant_time
            session.updated_at = datetime.now().astimezone().isoformat()
            _save_session(session)

            if not turn_result.error and turn_result.assistant_message:
                try:
                    (
                        session.last_extracted_index,
                        session.tokens_at_last_extraction,
                    ) = maybe_extract_memories(
                        session.messages,
                        cfg,
                        last_extracted_index=session.last_extracted_index,
                        tokens_at_last_extraction=session.tokens_at_last_extraction,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "memory extraction failed for session %s: %s",
                        session_id,
                        exc,
                    )

                if session.title is None:
                    # First successful assistant reply on this session — generate
                    # a short sidebar title. Bounded to a few seconds so a
                    # hanging LLM endpoint can't delay the SSE ``done`` frame
                    # indefinitely; on timeout/failure we leave title=None and
                    # the UI keeps falling back to preview/timestamp.
                    try:
                        title = await asyncio.wait_for(
                            asyncio.to_thread(_generate_chat_title, cfg, session.messages),
                            timeout=8.0,
                        )
                    except (TimeoutError, Exception) as exc:  # noqa: BLE001
                        logger.warning(
                            "chat title generation failed for session %s: %s",
                            session_id,
                            exc,
                        )
                        title = None
                    if title:
                        session.title = title
                        _save_session(session)

            if turn_result.error:
                await queue.put({"type": "error", "message": turn_result.error})
            else:
                done_event: dict[str, Any] = {"type": "done"}
                if turn_result.ttft_ms is not None:
                    done_event["ttft_ms"] = round(turn_result.ttft_ms, 1)
                await queue.put(done_event)
        except asyncio.CancelledError:
            # Client disconnected mid-turn; let cancellation propagate but
            # still terminate the queue so the generator can exit cleanly.
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("send_message failed for session %s", session_id)
            await queue.put({"type": "error", "message": str(exc)})
        finally:
            await queue.put(None)  # sentinel

    runner = asyncio.create_task(_run_and_persist())

    async def _events() -> AsyncIterator[dict[str, Any]]:
        try:
            while True:
                event = await queue.get()
                if event is None:
                    yield {"data": "[DONE]"}
                    return
                yield {"data": json.dumps(event, ensure_ascii=False)}
        finally:
            # Cover client disconnect (CancelledError) and normal exit alike.
            if not runner.done():
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await runner

    return EventSourceResponse(_events())


@router.post("/consolidate", response_model=ApiResponse, tags=["consolidation"])
def trigger_consolidation() -> ApiResponse:
    """手动触发一次离线 consolidation（绕过 session 计数器）。

    Returns the current completed-session counter so callers can sanity-check
    the cadence state.
    """
    from ..writer.classifier import trigger_consolidation_now

    cfg = _get_cfg()
    try:
        count = trigger_consolidation_now(cfg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("manual consolidation failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"consolidation failed: {exc}") from exc
    return ApiResponse(data={"status": "triggered", "session_count": count})


@router.delete("/chat/sessions/{session_id}", response_model=ApiResponse, tags=["chat"])
def delete_session(
    session_id: Annotated[str, FastPath(description="会话 ID（8 位短 UUID）")],
) -> ApiResponse:
    """删除指定会话及其持久化文件。"""
    with _sessions_lock:
        session = _sessions.pop(session_id, None)

    if session is None and not _session_path(session_id).exists():
        raise HTTPException(status_code=404, detail="session not found")

    _session_path(session_id).unlink(missing_ok=True)

    return ApiResponse(data={"success": True, "session_id": session_id, "archived": True})
