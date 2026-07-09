"""LLM analyzer — context pre-injection + web_search tool for meeting analysis."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import anthropic
import httpx
import jieba.analyse
from rich.console import Console

from .. import paths
from ..config import load as _load_config
from ..config import provider_api_key, provider_base_url
from ..intent import recall as intent_recall
from ..intent import sink as intent_sink
from ..intent.ontology import Intent, IntentEvidence
from ..intent.pack import SceneState
from ..store import fts
from ..writer.llm import _adapt, _bare_model, _to_anthropic_messages, _to_anthropic_tools
from .config import LLMConfig, TriggerConfig
from .store import TranscriptStore
from .transcript import Transcript

console = Console()


def _extract_keywords(text: str, topk: int = 8) -> list[str]:
    return list(jieba.analyse.extract_tags(text, topK=topk))


# Production recall flag defaults — used when config load fails so the meeting
# recall never silently falls back to the assemble_background signature defaults
# (all False = reads superseded 旧信念). Mirrors config.py defaults; the
# use_chain_index / read_evo_nodes staging flags were retired in PR-7.
_RECALL_FLAG_DEFAULTS: dict[str, bool] = {
    "fold_superseded": True,
    "chain_trail": True,
    "include_confidence": True,
}

# Main-layer recall budget default (#611) — mirrors ``IntentRecognizerConfig
# .recall_max_chars`` so a config-load failure still uses the post-ablation
# 2400 budget rather than the 1200 signature default that squeezed the fact
# layer in ~66% of calls.
_RECALL_MAX_CHARS_DEFAULT = 2400


def _recall_flags_from_config() -> dict[str, bool]:
    """Build the recall kwargs from ``[intent_recognizer] recall_*`` (#530).

    The meeting recall entry must honour the SAME toggles the timeline recognizer
    does; otherwise ``recall_fold_superseded`` config never takes effect on
    meeting analysis and it folds in superseded 旧信念. Falls back to the
    production defaults on any config error (never breaks a meeting push).
    """
    try:
        ik = _load_config().intent_recognizer
    except Exception:  # pragma: no cover — defensive; never break analysis
        return dict(_RECALL_FLAG_DEFAULTS)
    return {
        "fold_superseded": ik.recall_fold_superseded,
        "chain_trail": ik.recall_chain_trail,
        "include_confidence": ik.recall_include_confidence,
    }


def _recall_max_chars_from_config() -> int:
    """Main-layer recall budget from ``[intent_recognizer] recall_max_chars``.

    Kept separate from :func:`_recall_flags_from_config` (which is bool-only) so
    the meeting recall shares the SAME post-ablation 2400 budget as the slow
    path (#611); falls back to the default on any config error.
    """
    try:
        return _load_config().intent_recognizer.recall_max_chars
    except Exception:  # pragma: no cover — defensive; never break analysis
        return _RECALL_MAX_CHARS_DEFAULT


SYSTEM_PROMPT = """\
实时会议信息推送系统。单向推送，非对话。用户无法回复你。

# 输入
两路语音转写（可能有口误，结合上下文理解）：
- [会议] 其他参与者发言
- [用户] 当前用户发言

输入分为「背景上下文」和「>>> 新内容」两部分。**你只需要回应「>>> 新内容」中的内容**，背景上下文仅供理解语境，不要回答背景中的问题。

你还会收到自动检索的「用户记忆」和「会议历史」，这些是根据当前对话关键词预先搜索的结果。

# 工具（四个独立数据源，互不交叉）
- search_memory: 搜索结构化记忆（人物、项目、偏好、决策、工作习惯）。不含屏幕活动，不含会议记录。关键词用英文或中文专有名词。
- search_screen: 搜索用户电脑屏幕活动的AI摘要（用过什么应用、看过什么页面、编辑过什么文件）。用于"之前在电脑上看的"、"刚才打开的"类问题。**摘要主要为英文但含中文应用名/标题，同一个关键词要同时用中英文各查一次**（如"微信"和"WeChat"）。可只传date不传queries查看当天完整摘要。
- web_search: 联网搜索实时信息。用于需要最新数据、公开信息查证。
- search_meeting: 搜索本次和历史会议的对话转写内容。不含记忆，不含屏幕活动。

已预注入的记忆和会议历史通常够用，优先基于预注入内容判断。不够时再调工具。

# 工具调用规则（严格执行）
- 只有一次调用机会，把所有需要的工具一次性全调出来
- 所有关键词放在一次调用的queries数组里，不要一个关键词调一次
- 正确：search_memory(queries=["project-X", "项目X", "deadline", "截止日期"])
- 错误：先调search_memory(queries=["project-X"])，再调search_memory(queries=["deadline"])
- 需要多个工具时并行调用：search_memory(...) + web_search(...) 同时发出

# 推送条件
模糊数据需查证 | 用户需要参考答案 | 重要决策/行动项/截止日期 | 与用户历史相关的信息

# 输出
有信息：1-2句陈述性推送，不提问、不解释原因、不重复已推送内容。
无信息：[SILENT]
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": "搜索用户记忆库（英文摘要+中文专有名词）。预注入的记忆不够时调用，用英文关键词或中文专有名词搜索。",
            "parameters": {
                "type": "object",
                "properties": {
                    "queries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "搜索关键词列表",
                    }
                },
                "required": ["queries"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "联网搜索实时信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "queries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "搜索查询列表",
                    }
                },
                "required": ["queries"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_screen",
            "description": "搜索用户电脑屏幕活动的AI摘要。当用户提到'之前在电脑上看的'、'刚才打开的'、'那个页面/文件/网站'时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "queries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "搜索关键词列表。不传则返回该日期的完整摘要。",
                    },
                    "date": {
                        "type": "string",
                        "description": "限定日期，格式 YYYY-MM-DD。如'今天'、'昨天'传对应日期，不传则搜全部。",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_meeting",
            "description": "搜索本次和历史会议的对话内容。预注入的会议历史不够时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "queries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "搜索关键词列表",
                    }
                },
                "required": ["queries"],
            },
        },
    },
]


class MeetingAnalyzer:
    """Pre-injects memory/meeting context, only keeps web_search as tool."""

    _LOG_COLORS: dict[str, str] = {
        "trigger": "yellow",
        "keywords": "magenta",
        "prefetch": "cyan",
        "tool": "blue",
        "result": "dim",
        "push": "green bold",
        "silent": "dim",
        "error": "red",
    }

    def __init__(
        self,
        llm_config: LLMConfig,
        trigger_config: TriggerConfig,
        store: TranscriptStore,
        on_push: Callable[[str], None],
        log_file: Any = None,
        on_event: Callable[[dict[str, str]], None] | None = None,
    ):
        self._llm = llm_config
        self._trigger = trigger_config
        self._store = store
        self._on_push = on_push
        self._on_event = on_event
        self._log_file = log_file
        self._memory_db = self._open_memory_db()
        # Recall flag plumbing (#530): snapshot the production recall toggles once
        # so meeting recall reads current beliefs (folds superseded) like the
        # timeline recognizer, instead of the signature defaults (all False).
        self._recall_flags = _recall_flags_from_config()
        # Main-layer recall budget (#611): share the post-ablation 2400 default
        # with the slow path so meeting recall isn't starved at the legacy 1200.
        self._recall_max_chars = _recall_max_chars_from_config()
        # Single-worker analysis queue (#531): every transcript batch used to
        # spawn a bare daemon Thread, all sharing this one ``check_same_thread=
        # False`` SQLite connection with no concurrency bound — row_factory /
        # transaction state could interleave under high-frequency meeting batches.
        # A 1-worker pool serializes analysis onto one thread (still off the
        # caller's thread) so the shared connection is touched by one thread at a
        # time and back-pressure is bounded.
        self._analysis_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="meeting-analyze"
        )
        # §8 ① scene identity + ③ accumulated scene state. Deriving the scope
        # from the per-meeting db file gives every meeting a stable scene id, so
        # its recognized hints land in the unified intent stream under one scope.
        self._scope = "meeting-" + Path(self._store._db_path).stem
        self._scene = SceneState(scope=self._scope)

    @property
    def scope(self) -> str:
        """① The meeting scene's stable identity."""
        return self._scope

    @property
    def scene(self) -> SceneState:
        """③ The accumulated meeting scene state."""
        return self._scene

    def _persist_intent(self, text: str) -> None:
        """Project a surfaced meeting hint into the unified intent stream.

        This is what turns the meeting analyzer from an isolated push-only island
        into a ScenePack: every hint it surfaces is persisted (classifier-grade,
        same sink as every other recognizer) so it becomes part of main memory and
        is retrievable via ``search_memory``. Best-effort and fully additive — a
        failure here never affects the live push path.
        """
        text = text.strip()
        if not text:
            return
        try:
            intent = Intent(
                kind="meeting_hint",
                scope=self._scope,
                rationale=text[:200],
                ts=datetime.now().isoformat(timespec="seconds"),
                payload={"text": text},
                evidence=[
                    IntentEvidence(
                        source="meeting_transcript", ref_id=self._scope, quote=text[:120]
                    )
                ],
            )
            with fts.cursor() as conn:
                intent_sink.persist_intent(conn, intent)
            self._scene.note_surfaced(text)
        except Exception as e:  # pragma: no cover - defensive, never break a push
            self._log("error", f"persist intent failed: {e}")

    def _open_memory_db(self) -> sqlite3.Connection | None:
        # The same unified index.db the rest of the pipeline uses — honour
        # PERSOME_ROOT rather than hard-coding ~/.persome so tests
        # (and any relocated root) read the right database.
        db_path = paths.index_db()
        if not db_path.exists():
            return None
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def analyze(self, batch: list[Transcript]) -> None:
        # Serialize onto the single-worker pool (#531) instead of spawning a bare
        # thread per batch. Wrapped so an exception in analysis is logged, not
        # silently swallowed in an unretrieved Future.
        self._analysis_pool.submit(self._run_analyze_safe, batch)

    def _run_analyze_safe(self, batch: list[Transcript]) -> None:
        try:
            self._do_analyze(batch)
        except Exception as e:  # noqa: BLE001 — never let analysis kill the worker
            self._log("error", f"analyze failed: {e}")

    def close(self) -> None:
        """Stop the analysis worker (called from the assistant's _stop)."""
        with contextlib.suppress(Exception):
            self._analysis_pool.shutdown(wait=False, cancel_futures=True)

    def _do_analyze(self, batch: list[Transcript]) -> None:
        recent = self._store.get_recent(seconds=self._trigger.context_window_seconds)
        recent_pushes = self._store.get_recent_pushes(limit=5)

        batch_timestamps = {t.timestamp for t in batch}

        new_lines = []
        context_lines = []
        for r in recent:
            if not r["text"].strip():
                continue
            label = "会议" if r["source"] == "meeting" else "用户"
            line = f"[{label}] {r['text']}"
            if r["timestamp"] in batch_timestamps:
                new_lines.append(line)
            else:
                context_lines.append(line)

        if not new_lines:
            return

        new_text = " ".join(t.text for t in batch if t.text.strip())
        keywords = _extract_keywords(new_text)
        self._log("keywords", f"{keywords}")

        # Unified layered recall (P4): scene intents for this meeting + behavioural
        # priors + durable facts + keyword fallback — replaces the old flat
        # per-keyword FTS dump (_prefetch_memory).
        memory_context = ""
        if self._memory_db is not None:
            memory_context = intent_recall.assemble_background(
                self._memory_db,
                scope=self._scope,
                hints=keywords,
                max_chars=self._recall_max_chars,
                fold_superseded=self._recall_flags["fold_superseded"],
                chain_trail=self._recall_flags["chain_trail"],
                include_confidence=self._recall_flags["include_confidence"],
            )
        meeting_context = self._prefetch_meeting(keywords)
        if memory_context:
            self._log("prefetch", f"记忆命中: {memory_context[:200]}")
        if meeting_context:
            self._log("prefetch", f"会议命中: {meeting_context[:200]}")

        user_msg_parts = []
        if context_lines:
            user_msg_parts.append("背景上下文：")
            user_msg_parts.append("\n".join(context_lines))
            user_msg_parts.append("")

        user_msg_parts.append(">>> 新内容：")
        user_msg_parts.append("\n".join(new_lines))

        if memory_context:
            user_msg_parts.append(f"\n用户记忆（自动检索）：\n{memory_context}")
        if meeting_context:
            user_msg_parts.append(f"\n会议历史（自动检索）：\n{meeting_context}")

        if recent_pushes:
            user_msg_parts.append("\n已推送（不要重复）：")
            for p in recent_pushes:
                user_msg_parts.append(f"- {p}")

        user_msg = "\n".join(user_msg_parts)

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ]

        try:
            self._log("trigger", f"分析触发，上下文 {len(context_lines)} 条，关键词 {keywords}")

            response = self._complete(messages, tools=TOOLS)
            msg = response.choices[0].message

            if not msg.tool_calls:
                result = (msg.content or "").strip()
                if (
                    result
                    and result != "[SILENT]"
                    and "DSML" not in result
                    and "tool_calls" not in result
                    and "<|" not in result
                ):
                    self._log_file_only("push", result)
                    if self._on_event:
                        self._on_event({"type": "push", "message": result})
                    self._store.save_push(result)
                    self._persist_intent(result)
                    self._on_push(result)
                else:
                    self._log_file_only("silent", "无需推送")
                return

            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": msg.content or None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            }
            messages.append(assistant_msg)

            def _run_tool(tc: Any) -> tuple[str, str]:
                name = tc.function.name
                args = json.loads(tc.function.arguments)
                self._log("tool", f"{name}({args})")
                result = self._execute_tool(name, args)
                self._log("result", f"{name} → {result[:200]}")
                return tc.id, result

            with ThreadPoolExecutor(max_workers=len(msg.tool_calls)) as pool:
                futures = [pool.submit(_run_tool, tc) for tc in msg.tool_calls]
                for f in futures:
                    call_id, tool_result = f.result()
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": tool_result,
                        }
                    )

            self._stream_response(messages)

        except Exception as e:
            self._log("error", str(e))

    def _client(self) -> anthropic.Anthropic:
        """Anthropic SDK client (same ANTHROPIC_* creds / gateway as the chat + writer paths)."""
        return anthropic.Anthropic(
            api_key=provider_api_key("anthropic"),
            base_url=provider_base_url("anthropic"),
        )

    def _complete(self, messages: list[dict], *, tools: list[dict] | None = None) -> Any:
        """One-shot Anthropic call, adapted back to the OpenAI-shape this file parses."""
        system, amsgs = _to_anthropic_messages(messages)
        kwargs: dict[str, Any] = {
            "model": _bare_model(self._llm.model),
            "messages": amsgs,
            "max_tokens": self._llm.max_tokens,
        }
        if system is not None:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = _to_anthropic_tools(tools)
        return _adapt(self._client().messages.create(**kwargs))

    def _stream_response(self, messages: list[dict]) -> None:
        system, amsgs = _to_anthropic_messages(messages)
        full = []
        started = False
        stream_kwargs: dict[str, Any] = {
            "model": _bare_model(self._llm.model),
            "messages": amsgs,
            "max_tokens": self._llm.max_tokens,
        }
        if system is not None:
            stream_kwargs["system"] = system
        with self._client().messages.stream(**stream_kwargs) as stream:
            for delta in stream.text_stream:
                if not delta:
                    continue
                full.append(delta)
                current = "".join(full)
                if not started and (
                    "DSML" in current or "tool_calls" in current or "<|" in current
                ):
                    continue
                started = True
                if self._on_event:
                    self._on_event({"type": "push_chunk", "message": delta})
        result = "".join(full).strip()
        if result and result != "[SILENT]" and "DSML" not in result and "tool_calls" not in result:
            if self._on_event:
                self._on_event({"type": "push_end", "message": ""})
            self._log_file_only("push", result)
            self._store.save_push(result)
            self._persist_intent(result)
            self._on_push(result)
        else:
            if started and self._on_event:
                self._on_event({"type": "push_end", "message": ""})
            self._log("silent", "无需推送")

    def _prefetch_meeting(self, keywords: list[str]) -> str:
        if not keywords:
            return ""
        parts = []
        for kw in keywords:
            try:
                results = self._store.search(kw, limit=3)
                for r in results:
                    label = "会议" if r["source"] == "meeting" else "用户"
                    parts.append(f"[{label}] {r['text']}")
            except Exception:
                pass
        return "\n".join(parts) if parts else ""

    def _execute_tool(self, name: str, args: dict) -> str:
        if name == "search_memory":
            return self._tool_search_memory(args["queries"])
        elif name == "search_screen":
            return self._tool_search_screen(args.get("queries", []), args.get("date"))
        elif name == "web_search":
            return self._tool_web_search(args["queries"])
        elif name == "search_meeting":
            return self._tool_search_meeting(args["queries"])
        return f"Unknown tool: {name}"

    def _tool_search_screen(self, queries: list[str], date: str | None = None) -> str:
        if not self._memory_db:
            return "屏幕记录不可用"
        path_filter = f"event-{date}%" if date else "event-%"
        all_parts = []
        if not queries:
            try:
                rows = self._memory_db.execute(
                    "SELECT path, content FROM entries WHERE path LIKE ? ORDER BY path DESC LIMIT 5",
                    (path_filter,),
                ).fetchall()
                if rows:
                    for path, content in rows:
                        all_parts.append(f"[{path}]\n{content[:800]}")
                else:
                    all_parts.append("未找到")
            except Exception as e:
                all_parts.append(f"搜索出错: {e}")
        else:
            for query in queries:
                try:
                    rows = self._memory_db.execute(
                        "SELECT path, content FROM entries WHERE entries MATCH ? "
                        "AND path LIKE ? ORDER BY rank LIMIT 5",
                        (query, path_filter),
                    ).fetchall()
                    all_parts.append(f"== {query} ==")
                    if rows:
                        for path, content in rows:
                            all_parts.append(f"[{path}]\n{content[:500]}")
                    else:
                        all_parts.append("未找到")
                except Exception as e:
                    all_parts.append(f"== {query} ==\n搜索出错: {e}")
        return "\n---\n".join(all_parts)

    def _tool_search_memory(self, queries: list[str]) -> str:
        if not self._memory_db:
            return "记忆库不可用"
        all_parts = []
        for query in queries:
            try:
                rows = self._memory_db.execute(
                    "SELECT path, content FROM entries WHERE entries MATCH ? "
                    "AND path NOT LIKE 'event-%' ORDER BY rank LIMIT 3",
                    (query,),
                ).fetchall()
                if rows:
                    all_parts.append(f"== {query} ==")
                    for path, content in rows:
                        all_parts.append(f"[{path}]\n{content[:500]}")
                else:
                    all_parts.append(f"== {query} ==\n未找到")
            except Exception as e:
                all_parts.append(f"== {query} ==\n搜索出错: {e}")
        return "\n---\n".join(all_parts)

    def _tool_search_meeting(self, queries: list[str]) -> str:
        all_parts = []
        for query in queries:
            try:
                current = self._store.search(query, limit=5)
                history = self._store.search_history(query, limit=5)
                all_parts.append(f"== {query} ==")
                if current:
                    all_parts.append("[本次会议]")
                    for r in current:
                        label = "会议" if r["source"] == "meeting" else "用户"
                        all_parts.append(f"[{label}] {r['text']}")
                if history:
                    all_parts.append("[历史会议]")
                    for r in history:
                        label = "会议" if r["source"] == "meeting" else "用户"
                        all_parts.append(f"[{label}] ({r['meeting']}) {r['text']}")
                if not current and not history:
                    all_parts.append("未找到")
            except Exception as e:
                all_parts.append(f"== {query} ==\n搜索出错: {e}")
        return "\n---\n".join(all_parts)

    # Bocha web-search creds are env-driven (bring your own key): no key ships in
    # the source tree. When BOCHA_API_KEY is unset the web-search tool degrades
    # gracefully (per-query "not configured" line) — the rest of the analyzer is
    # unaffected.
    _BOCHA_SEARCH_URL = "https://api.bochaai.com/v1/web-search"

    def _tool_web_search(self, queries: list[str]) -> str:
        api_key = os.environ.get("BOCHA_API_KEY", "")
        if not api_key:
            return "\n---\n".join(
                f"== {q} ==\nweb search not configured (set BOCHA_API_KEY)" for q in queries
            )

        def _search_one(query: str) -> str:
            try:
                resp = httpx.post(
                    self._BOCHA_SEARCH_URL,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"query": query, "count": 3},
                    timeout=10,
                )
                data = resp.json()
                parts = [f"== {query} =="]
                pages = data.get("data", {}).get("webPages", {}).get("value", [])
                if pages:
                    for p in pages:
                        title = p.get("name", "")
                        snippet = p.get("snippet", "")[:300]
                        parts.append(f"[{title}]\n{snippet}")
                else:
                    parts.append("未找到相关结果")
                return "\n".join(parts)
            except Exception as e:
                return f"== {query} ==\n搜索出错: {e}"

        with ThreadPoolExecutor(max_workers=len(queries)) as pool:
            results = list(pool.map(_search_one, queries))
        return "\n---\n".join(results)

    _SSE_TAGS = frozenset({"error", "system"})

    def _log(self, tag: str, msg: str) -> None:
        self._log_file_only(tag, msg)
        if self._on_event and tag in self._SSE_TAGS:
            self._on_event({"type": tag, "message": msg})

    def _log_file_only(self, tag: str, msg: str) -> None:
        color = self._LOG_COLORS.get(tag, "white")
        console.print(f"[{color}][{tag}][/{color}] {msg}")
        if self._log_file:
            ts = datetime.now().strftime("%H:%M:%S")
            self._log_file.write(f"[{ts}] [{tag}] {msg}\n")
            self._log_file.flush()
