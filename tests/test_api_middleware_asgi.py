"""The API middleware must stay *pure ASGI* — never Starlette BaseHTTPMiddleware.

``BaseHTTPMiddleware`` pumps the response body through an anyio memory stream, which
breaks long-lived streaming responses: a client disconnect
surfaces as ``RuntimeError("No response returned")`` and uvicorn logs a spurious HTTP
500. These tests pin the transport behavior without depending on a product event route.
These tests pin the fix: (1) the API app carries no ``BaseHTTPMiddleware``, and (2) a
streaming response passes cleanly *through* the three pure-ASGI middleware. The
middleware's actual behaviour (trace id, access log, origin/host guard) stays covered
by ``test_trace_middleware.py`` and ``test_api_origin_guard.py``.
"""

from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import StreamingResponse

from persome.api import (
    _AccessLogMiddleware,
    _OriginGuardMiddleware,
    _TraceIdMiddleware,
    build_api_app,
)


def test_api_app_uses_no_basehttpmiddleware() -> None:
    """Guard against re-introducing the SSE-breaking BaseHTTPMiddleware."""
    app = build_api_app(auth_enabled=False)
    classes = [mw.cls for mw in app.user_middleware]
    assert BaseHTTPMiddleware not in classes, f"BaseHTTPMiddleware reintroduced: {classes}"
    # …and the three pure-ASGI replacements are all present.
    assert _AccessLogMiddleware in classes
    assert _TraceIdMiddleware in classes
    assert _OriginGuardMiddleware in classes


def test_origin_guard_is_outermost() -> None:
    """A rejected request must short-circuit before trace_id / access_log run, so
    the guard has to be the outermost user middleware (last added → index 0)."""
    app = build_api_app(auth_enabled=False)
    assert app.user_middleware[0].cls is _OriginGuardMiddleware


def test_sse_streams_through_all_three_middleware() -> None:
    """A finite SSE-style response passes cleanly through the pure-ASGI stack and
    arrives intact — the streaming case BaseHTTPMiddleware buffered/broke."""
    app = FastAPI()
    app.add_middleware(_AccessLogMiddleware)
    app.add_middleware(_TraceIdMiddleware)
    app.add_middleware(_OriginGuardMiddleware, require_local_origin=True)

    @app.get("/sse")
    async def _sse() -> StreamingResponse:
        async def _gen():
            yield f"data: {json.dumps({'n': 1})}\n\n"
            yield f"data: {json.dumps({'n': 2})}\n\n"

        return StreamingResponse(_gen(), media_type="text/event-stream")

    client = TestClient(app, headers={"host": "127.0.0.1"})
    resp = client.get("/sse")
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    body = resp.text
    assert '{"n": 1}' in body
    assert '{"n": 2}' in body
