"""HTTP server — start/stop meeting assistant via REST API + SSE log stream."""

from __future__ import annotations

import contextlib
import json
import queue
import threading
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

from rich.console import Console

from .assistant import MeetingAssistant
from .config import MeetingConfig

console = Console()


class _State:
    def __init__(self) -> None:
        self.assistant: MeetingAssistant | None = None
        self.thread: threading.Thread | None = None
        self._subscribers: list[queue.Queue[dict[str, str]]] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue[dict[str, str]]:
        q: queue.Queue[dict[str, str]] = queue.Queue(maxsize=200)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[dict[str, str]]) -> None:
        with self._lock:
            self._subscribers = [s for s in self._subscribers if s is not q]

    def broadcast(self, event: dict[str, str]) -> None:
        with self._lock:
            for q in self._subscribers:
                with contextlib.suppress(queue.Full):
                    q.put_nowait(event)

    # -- assistant 生命周期：全部走 _lock，消除 /transcript /start /stop 间的 TOCTOU --
    # （issue #439：判空与使用 / 置空之间无锁，并发会在 None 上解引用）

    def _is_running_locked(self) -> bool:
        return self.assistant is not None and self.thread is not None and self.thread.is_alive()

    def start(self, factory: Callable[[], MeetingAssistant]) -> bool:
        """原子启动：已在跑则返回 False；否则建 assistant + 线程并 start，返回 True。"""
        with self._lock:
            if self._is_running_locked():
                return False
            assistant = factory()
            thread = threading.Thread(target=assistant.run, daemon=True)
            self.assistant = assistant
            self.thread = thread
            thread.start()
            return True

    def stop(self) -> MeetingAssistant | None:
        """原子停止：取出并清空当前 assistant（置 _running=False）；None 表示本来没在跑。"""
        with self._lock:
            a = self.assistant
            if a is None:
                return None
            a._running = False
            self.assistant = None
            self.thread = None
            return a

    def current(self) -> MeetingAssistant | None:
        """取当前 assistant 的快照引用——调用方拿到后即使被并发 stop 置空也不再解引用 None。"""
        with self._lock:
            return self.assistant


def _json_response(handler: BaseHTTPRequestHandler, status: int, body: dict[str, Any]) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.end_headers()
    handler.wfile.write(json.dumps(body, ensure_ascii=False).encode())


def _make_handler(state: _State) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        _state: ClassVar[_State] = state

        def do_GET(self) -> None:
            if self.path == "/events":
                self._handle_sse()
            else:
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path == "/start":
                self._handle_start()
            elif self.path == "/stop":
                self._handle_stop()
            elif self.path == "/transcript":
                self._handle_transcript()
            else:
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not found"})

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            return dict(json.loads(raw))

        def _handle_start(self) -> None:
            self._read_json()  # drain any request body — analysis takes no params
            config = MeetingConfig()
            started = self._state.start(
                lambda: MeetingAssistant(config, on_event=self._state.broadcast)
            )
            if not started:
                _json_response(self, HTTPStatus.CONFLICT, {"error": "already running"})
                return
            _json_response(self, HTTPStatus.OK, {"status": "started"})

        def _handle_transcript(self) -> None:
            # 取快照引用：即使并发 _handle_stop 把 assistant 置空，这里用的还是旧引用，
            # 不会在 None 上解引用（issue #439）。
            assistant = self._state.current()
            if assistant is None:
                self.send_response(204)
                self.end_headers()
                return
            body = self._read_json()
            text = body.get("text", "")
            source = body.get("source", "meeting")
            is_final = bool(body.get("is_final", True))
            sentence_id = int(body.get("sentence_id", 0))
            if text:
                assistant.feed_transcript(
                    source=source,
                    text=text,
                    is_final=is_final,
                    sentence_id=sentence_id,
                )
            self.send_response(204)
            self.end_headers()

        def _handle_stop(self) -> None:
            if self._state.stop() is None:
                _json_response(self, HTTPStatus.CONFLICT, {"error": "not running"})
                return
            self._state.broadcast({"type": "system", "message": "Meeting stopped"})
            _json_response(self, HTTPStatus.OK, {"status": "stopped"})

        def _handle_sse(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            q = self._state.subscribe()
            try:
                while True:
                    try:
                        event = q.get(timeout=15)
                        data = json.dumps(event, ensure_ascii=False)
                        self.wfile.write(f"data: {data}\n\n".encode())
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                self._state.unsubscribe(q)

        def log_message(self, format: str, *args: Any) -> None:
            console.print(f"[dim][http] {args[0]}[/dim]")

    return Handler


def _start_parent_death_watch() -> None:
    """Self-terminate if the parent process (the app) dies.

    The Mens.app starts this server as a child. On a hard quit (Cmd+Q / tray
    Exit / crash) the app can't run its graceful teardown, which would leave
    this server orphaned (reparented to launchd, so ``getppid()`` becomes 1).
    Poll for that and exit, so we never linger after the app is gone.
    """
    import os
    import time

    def _watch() -> None:
        while True:
            if os.getppid() == 1:
                os._exit(0)
            time.sleep(2)

    threading.Thread(target=_watch, daemon=True).start()


def run_server(host: str = "127.0.0.1", port: int = 8750) -> None:
    state = _State()
    handler = _make_handler(state)

    _start_parent_death_watch()

    server = ThreadingHTTPServer((host, port), handler)
    console.print(f"[bold green]Meeting API server listening on {host}:{port}[/bold green]")
    console.print("  POST /start  — begin analysis (transcripts arrive from the app)")
    console.print("  POST /stop")
    console.print(
        '  POST /transcript — {"source": "meeting|user", "text": "...", "is_final": true}'
    )
    console.print("  GET  /events — SSE push stream")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if state.assistant:
            state.assistant._running = False
        server.shutdown()
        console.print("\n[yellow]Server stopped.[/yellow]")
