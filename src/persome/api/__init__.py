"""Persome HTTP REST API.

FastAPI application mounted at root ``/`` inside the MCP server's
Starlette app via FastMCP ``custom_route`` / manual ``Mount``.
"""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.routing import Mount
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .. import __version__
from ..config import Config
from ..logger import get as _get_logger
from ..security.auth import LocalAPIAuthMiddleware
from ..security.body_limit import (
    RequestBodyLimitMiddleware,
    RequestConcurrencyLimitMiddleware,
)
from ..trace import generate_trace_id, set_trace_id
from .routes import router
from .routes import set_config as _set_route_config

_access_logger = _get_logger("persome.api.access")

# ── Local-origin guard helpers (see _OriginGuardMiddleware) ───────────────────
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def _hostname_of(host: str | None) -> str | None:
    """Lower-cased hostname with any ``:port`` stripped; ``[::1]`` kept whole."""
    if not host:
        return None
    if host.startswith("["):
        hostname = host.split("]", 1)[0] + "]" if "]" in host else host
    else:
        hostname = host.rsplit(":", 1)[0] if ":" in host else host
    return hostname.strip().lower() or None


def _is_local_host(host: str | None) -> bool:
    return _hostname_of(host) in _LOCAL_HOSTS


def _is_rebinding_host(host: str | None) -> bool:
    """Whether a Host header looks like a routable public name (rebinding).

    The loopback-only DNS-rebinding threat is an attacker page on a *resolvable*
    domain (``evil.com`` rebound to 127.0.0.1) — those always carry a TLD dot. A
    bare single-label host (Starlette's ``testserver``, a container name) is not a
    public attack vector, so only a non-local Host that contains a dot is treated
    as a rebinding attempt.
    """
    hostname = _hostname_of(host)
    if hostname is None or hostname in _LOCAL_HOSTS:
        return False
    return "." in hostname


# ── Middleware: PURE ASGI, deliberately NOT BaseHTTPMiddleware ────────────────
# Starlette's ``BaseHTTPMiddleware`` / ``@app.middleware("http")`` pumps the
# response body through an anyio memory stream, which is incompatible with a
# long-lived streaming response: when the client
# disconnects, the pump raises ``anyio.EndOfStream`` / ``CancelledError`` and the
# middleware ends with ``RuntimeError: No response returned``, which uvicorn logs
# as a spurious HTTP 500. Pure ASGI middleware passes
# ``receive`` / ``send`` straight through, so a streaming disconnect is a clean
# cancellation the route handles — no synthetic 500. Keep these pure ASGI; do not
# reintroduce BaseHTTPMiddleware on this app (tests/test_api_middleware_asgi.py).


class _AccessLogMiddleware:
    """4xx/5xx access trail to ``~/.persome/logs/api.log``.

    Uvicorn's own access log isn't wired (this app is mounted into FastMCP's
    Starlette rather than launched via ``uvicorn.run(...)``), so this is the only
    honest access trail. Filter to keep api.log signal/noise sane:
      - 4xx / 5xx anywhere    → WARNING   (always interesting)
      - 2xx / 3xx             → skip      (eg. /health polling, /status)
    Status is sniffed off the ``http.response.start`` ASGI message.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.monotonic()
        status = 0

        async def _send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        await self.app(scope, receive, _send)
        elapsed_ms = (time.monotonic() - start) * 1000
        path = scope["path"]
        method = scope["method"]
        if status >= 400:
            _access_logger.warning("%s %s -> %d (%.0fms)", method, path, status, elapsed_ms)


class _TraceIdMiddleware:
    """Bind a request-scoped trace id (``X-Trace-Id`` header or a generated one)
    into the ContextVar for cross-layer log correlation, then clear it.

    Pure ASGI keeps the same execution context as the route (BaseHTTPMiddleware
    spawned a task, which broke ContextVar propagation), so the id set here is
    reliably visible to the handler and the access logger.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        tid = headers.get("x-trace-id") or generate_trace_id()
        set_trace_id(tid)
        try:
            await self.app(scope, receive, send)
        finally:
            set_trace_id("")


class _OriginGuardMiddleware:
    """Origin / Host guard (CSRF-to-localhost + DNS-rebinding hardening).

    The API binds 127.0.0.1 only, but a malicious web page can still
    ``fetch('http://127.0.0.1:8742/...')`` (CSRF) and a rebound DNS name can
    smuggle a non-local Host past a naive same-origin assumption. So:
      - any request carrying a browser ``Origin`` whose host isn't local → 403
      - any request whose ``Host`` is a routable public name → 403 (rebinding)
    Same-origin / native-client requests send no ``Origin`` and a local ``Host``,
    so they pass untouched; ``/health`` is always allowed so liveness probes can't
    be tripped. ``require_local_origin=False`` flips it off.

    Registered OUTERMOST (see build_api_app) so a rejected request never reaches
    trace_id / access_log / the route handlers.
    """

    def __init__(self, app: ASGIApp, *, require_local_origin: bool = True) -> None:
        self.app = app
        self.require_local_origin = require_local_origin

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.require_local_origin:
            await self.app(scope, receive, send)
            return
        # Always-allowed liveness probe → pass through.
        if scope["path"] == "/health":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        # Browser-supplied Origin: reject when its host isn't local. ``Origin: null``
        # (sandboxed iframe / file:// / data: / opaque origin) has no local host, so it
        # is rejected too — exactly the attacker-controlled contexts a CSRF guard must
        # not allowlist. Native/same-origin callers send NO Origin (the `if origin`
        # guard below), so this never trips them.
        origin = headers.get("origin")
        if origin and not _is_local_host(urlsplit(origin).hostname):
            await JSONResponse(
                {"success": False, "error": "forbidden: non-local Origin"},
                status_code=403,
                headers={"Connection": "close", "Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        # Host header: reject a routable public hostname (DNS-rebinding guard).
        if _is_rebinding_host(headers.get("host")):
            await JSONResponse(
                {"success": False, "error": "forbidden: non-local Host"},
                status_code=403,
                headers={"Connection": "close", "Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_api_app(cfg: Config | None = None, *, auth_enabled: bool = True) -> FastAPI:
    """Construct the FastAPI sub-application.

    Authentication is on by default.  In-process tests and schema rendering may
    disable it explicitly; the daemon mount never does.
    """
    app = FastAPI(
        title="Persome API",
        description="Local-first screen-context memory and personal-model REST API.",
        version=__version__,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # Six pure-ASGI middleware (NOT BaseHTTPMiddleware — it breaks streaming
    # responses with spurious 500s on disconnect; see the class docs).
    # Starlette applies middleware LIFO by add order, so the LAST added is
    # OUTERMOST. Add innermost→outermost: access_log (closest to the route, so it
    # sees the final status, incl. ExceptionMiddleware-converted 4xx) → trace_id
    # (ContextVar set before access_log logs) → body limit → concurrency
    # limit → auth → origin_guard.  Authentication stays outside body
    # buffering, so an unauthorized large request is rejected immediately.
    # ``api_require_local_origin`` defaults to on via ``getattr`` so a Config
    # without the field still hardens by default.
    require_local_origin = (
        getattr(cfg, "api_require_local_origin", True) if cfg is not None else True
    )
    app.add_middleware(_AccessLogMiddleware)
    app.add_middleware(_TraceIdMiddleware)
    app.add_middleware(RequestBodyLimitMiddleware)
    app.add_middleware(RequestConcurrencyLimitMiddleware)
    if auth_enabled:
        app.add_middleware(LocalAPIAuthMiddleware)
    app.add_middleware(_OriginGuardMiddleware, require_local_origin=require_local_origin)

    app.include_router(router)
    # FastAPI's lazy openapi() regeneration can misidentify list[str] query
    # params as requestBody. Lock the schema unconditionally at build time so
    # the runtime /openapi.json endpoint always serves the correct version.
    # Middleware does not automatically appear in OpenAPI, so declare the
    # production bearer boundary explicitly and exempt only public liveness.
    schema = app.openapi()
    components = schema.setdefault("components", {})
    security_schemes = components.setdefault("securitySchemes", {})
    security_schemes["LocalBearer"] = {
        "type": "http",
        "scheme": "bearer",
        "description": "Owner-local token from <PERSOME_ROOT>/env",
    }
    schema["security"] = [{"LocalBearer": []}]
    schema["paths"]["/health"]["get"]["security"] = []
    app.openapi_schema = schema
    return app


def render_openapi_json() -> str:
    """Render the canonical openapi.json string.

    Single source of truth for both ``scripts/regen_openapi.py`` (which
    overwrites the committed file) and ``tests/test_openapi_drift.py``
    (which fails CI if the committed file diverges).
    """
    cfg = Config()
    _set_route_config(cfg)
    app = build_api_app(cfg, auth_enabled=False)
    return json.dumps(app.openapi_schema, indent=2, ensure_ascii=False) + "\n"


def register_routes(
    server: Any,
    cfg: Config | None = None,
    *,
    auth_enabled: bool = True,
) -> None:
    """Mount the FastAPI app at root ``/`` on the FastMCP Starlette server.

    Called from :func:`persome.mcp.server.build_server` before the
    server is returned.
    """
    from .routes import set_config

    # Wire config so endpoints can resolve settings without re-reading disk
    set_config(cfg)

    api_app = build_api_app(cfg, auth_enabled=auth_enabled)
    server._custom_starlette_routes.append(Mount("", app=api_app))
