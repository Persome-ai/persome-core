from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from persome.config import Config, ModelConfig
from persome.llm_setup import probe_profile
from persome.providers import make_profile
from persome.writer.llm import call_llm, extract_text


class _Handler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []

    def log_message(self, format: str, *args: Any) -> None:
        return None

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        self.requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": body,
            }
        )
        if body.get("stream"):
            self._stream(body)
        else:
            self._json_completion(body)

    def _json_completion(self, body: dict[str, Any]) -> None:
        message: dict[str, Any] = {"role": "assistant", "content": "ok"}
        finish_reason = "stop"
        if body.get("tools"):
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_probe",
                        "type": "function",
                        "function": {"name": "persome_setup_check", "arguments": "{}"},
                    }
                ],
            }
            finish_reason = "tool_calls"
        payload = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        }
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _stream(self, body: dict[str, Any]) -> None:
        has_tool_result = any(message.get("role") == "tool" for message in body["messages"])
        if has_tool_result:
            delta: dict[str, Any] = {"role": "assistant", "content": "tool result received"}
            finish_reason = "stop"
        else:
            delta = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": '{"query":"project"}'},
                    }
                ],
            }
            finish_reason = "tool_calls"
        chunk = {
            "id": "chatcmpl-stream",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason,
                }
            ],
        }
        raw = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@contextmanager
def _server() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    _Handler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}/v1", _Handler.requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_writer_uses_real_openai_sdk_over_configured_endpoint(monkeypatch) -> None:
    monkeypatch.delenv("PERSOME_LLM_MOCK", raising=False)
    monkeypatch.setenv("PERSOME_LLM_API_KEY", "wire-secret")
    with _server() as (base_url, requests):
        cfg = Config(
            models={
                "default": ModelConfig(
                    provider="custom-openai",
                    protocol="openai",
                    model="test-model",
                    base_url=base_url,
                    api_key_env="PERSOME_LLM_API_KEY",
                )
            }
        )

        response = call_llm(
            cfg,
            "timeline",
            messages=[{"role": "user", "content": "hello"}],
        )

    assert extract_text(response) == "ok"
    assert requests[0]["path"] == "/v1/chat/completions"
    assert requests[0]["authorization"] == "Bearer wire-secret"


def test_onboarding_probe_uses_real_openai_sdk(monkeypatch) -> None:
    monkeypatch.setenv("PERSOME_LLM_API_KEY", "wire-secret")
    with _server() as (base_url, requests):
        profile = make_profile(
            "custom-openai",
            model="test-model",
            base_url=base_url,
            api_key_env="PERSOME_LLM_API_KEY",
            api_key="wire-secret",
            protocol="openai",
        )

        result = probe_profile(profile)

    assert result.completion_ok is True
    assert result.tool_call_ok is True
    assert len(requests) == 2
    assert requests[1]["body"]["tool_choice"]["function"]["name"] == "persome_setup_check"
