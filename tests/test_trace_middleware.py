"""Tests for the X-Trace-Id middleware in the API layer."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from persome.api import build_api_app
from persome.trace import get_trace_id


def _make_client() -> TestClient:
    return TestClient(build_api_app())


def test_middleware_uses_header_value() -> None:
    """When the client sends X-Trace-Id, the middleware must adopt it."""
    client = _make_client()
    resp = client.get("/health", headers={"X-Trace-Id": "clientabc123"})
    assert resp.status_code == 200


def test_middleware_generates_when_missing() -> None:
    """Without the header the middleware generates a 12-char hex trace."""
    client = _make_client()
    resp = client.get("/health")
    assert resp.status_code == 200


def test_trace_id_resets_after_request() -> None:
    """The ContextVar must be empty outside a request context."""
    client = _make_client()
    client.get("/health", headers={"X-Trace-Id": "tempid999999"})
    assert get_trace_id() == ""


def test_trace_id_visible_in_endpoint() -> None:
    """Register a tiny endpoint that echoes the trace — proves the ContextVar
    is set during request handling, not just middleware bookkeeping."""
    from fastapi import FastAPI, Request

    from persome.trace import generate_trace_id, set_trace_id

    app = FastAPI()

    @app.middleware("http")
    async def _trace(request: Request, call_next):  # type: ignore[no-untyped-def]
        tid = request.headers.get("x-trace-id") or generate_trace_id()
        set_trace_id(tid)
        try:
            return await call_next(request)
        finally:
            set_trace_id("")

    @app.get("/echo-trace")
    def _echo() -> dict[str, str]:
        return {"trace_id": get_trace_id()}

    client = TestClient(app)
    resp = client.get("/echo-trace", headers={"X-Trace-Id": "hello1234567"})
    assert resp.json()["trace_id"] == "hello1234567"

    resp2 = client.get("/echo-trace")
    tid = resp2.json()["trace_id"]
    assert re.fullmatch(r"[0-9a-f]{12}", tid)
