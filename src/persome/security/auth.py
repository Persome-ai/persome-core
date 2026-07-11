"""One bearer-token boundary for daemon-hosted HTTP, REST, and MCP.

The daemon's owner-only env file is loaded into :mod:`os.environ` before the
runtime starts.  This module deliberately reads only ``PERSOME_LOCAL_API_TOKEN``
for HTTP authentication; it neither reuses nor derives credentials from an
unrelated secret such as the screenshot-encryption key.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
import threading
import time

from starlette.applications import Starlette
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..env_file import LOCAL_API_TOKEN_ENV, is_valid_local_api_token

BROWSER_BOOTSTRAP_PATH = "/auth/browser-bootstrap"
BROWSER_SESSION_COOKIE = "persome_model_session"
BROWSER_BOOTSTRAP_TTL_SECONDS = 60
BROWSER_SESSION_TTL_SECONDS = 8 * 60 * 60
_MAX_BROWSER_BOOTSTRAP_NONCES = 64
_MAX_BROWSER_SESSIONS = 64

_browser_auth_lock = threading.Lock()
_browser_bootstrap_nonces: dict[str, float] = {}
_browser_sessions: dict[str, tuple[float, str]] = {}
_VIEWER_AUTHENTICATED = object()


class LocalAPIConfigurationError(RuntimeError):
    """The local HTTP boundary cannot be secured with the current environment."""


def _validated_token(raw: str | None) -> str | None:
    """Return a usable opaque bearer token, or ``None`` for a missing value."""
    return raw if is_valid_local_api_token(raw) else None


def local_api_token(*, required: bool = True) -> str | None:
    """Read the daemon's opaque local bearer token from the environment.

    ``required=True`` is the safe default for clients: an absent or malformed
    value raises instead of producing an unauthenticated request.  Server
    middleware uses ``required=False`` so it can keep the minimal ``/health``
    probe alive while returning 503 for every protected route.
    """
    token = _validated_token(os.environ.get(LOCAL_API_TOKEN_ENV))
    if token is None and required:
        raise LocalAPIConfigurationError(
            f"{LOCAL_API_TOKEN_ENV} must contain a single-line bearer token "
            "between 32 and 512 bytes"
        )
    return token


def auth_headers(token: str | None = None) -> dict[str, str]:
    """Return the canonical Authorization header for local REST/MCP clients."""
    resolved = local_api_token() if token is None else _validated_token(token)
    if resolved is None:
        raise LocalAPIConfigurationError("local API bearer token is empty or malformed")
    return {"Authorization": f"Bearer {resolved}"}


def _is_loopback_bind_host(host: str) -> bool:
    candidate = host.strip().lower()
    if candidate == "localhost":
        return True
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    # IPv6 bind strings may carry an interface scope.  It does not change
    # whether the underlying address is loopback.
    candidate = candidate.split("%", 1)[0]
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def loopback_http_url(host: str, port: int, path: str = "") -> str:
    """Build a client URL that cannot send the local bearer off-machine.

    Wildcard bind addresses are mapped to IPv4 loopback. Explicit non-loopback
    interfaces are rejected because the Runtime serves plain HTTP and its
    owner credential must never be forwarded across a network.
    """
    candidate = host.strip().lower()
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    candidate = candidate.split("%", 1)[0]
    if candidate in {"0.0.0.0", "::"}:
        candidate = "127.0.0.1"
    elif candidate != "localhost":
        try:
            if not ipaddress.ip_address(candidate).is_loopback:
                raise ValueError
        except ValueError as exc:
            raise LocalAPIConfigurationError(
                f"refusing to send the local API bearer to non-loopback host {host!r}"
            ) from exc
    if not 1 <= int(port) <= 65535:
        raise LocalAPIConfigurationError(f"invalid local API port: {port!r}")
    if path and (not path.startswith("/") or path.startswith("//")):
        raise LocalAPIConfigurationError(f"invalid local API path: {path!r}")
    rendered_host = f"[{candidate}]" if ":" in candidate else candidate
    return f"http://{rendered_host}:{int(port)}{path}"


def validate_bind_host(host: str) -> None:
    """Reject every bind address that is not strictly loopback.

    The Runtime serves plain HTTP. A bearer token prevents unauthorised use but
    does not prevent a network observer from stealing and replaying that token,
    so authenticated wildcard/LAN binds are unsafe too. Remote access belongs
    behind a separately authenticated, encrypted tunnel.
    """
    if _is_loopback_bind_host(host):
        return
    raise LocalAPIConfigurationError(
        f"refusing non-loopback bind {host!r}; Persome's local HTTP server has no TLS"
    )


def _has_valid_bearer(headers: Headers, expected: str) -> bool:
    values = headers.getlist("authorization")
    if len(values) != 1:
        return False
    scheme, separator, supplied = values[0].partition(" ")
    if not separator or scheme.lower() != "bearer" or not supplied:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _exact_raw_path(scope: Scope, expected: str) -> bool:
    """Match one canonical ASCII path, including its undecoded wire spelling."""
    if scope.get("path") != expected:
        return False
    raw_path = scope.get("raw_path")
    return raw_path is None or raw_path == expected.encode("ascii")


def _is_public_http_request(scope: Scope) -> bool:
    if scope.get("type") != "http" or scope.get("method", "").upper() != "GET":
        return False
    return _exact_raw_path(scope, "/health") or _exact_raw_path(scope, BROWSER_BOOTSTRAP_PATH)


def _prune_expired(values: dict[str, float], now: float) -> None:
    for value, expires_at in tuple(values.items()):
        if expires_at <= now:
            values.pop(value, None)


def _insert_bounded(
    values: dict[str, float],
    value: str,
    expires_at: float,
    *,
    maximum: int,
) -> None:
    if len(values) >= maximum:
        oldest = min(values, key=values.__getitem__)
        values.pop(oldest, None)
    values[value] = expires_at


def issue_browser_bootstrap_nonce(*, ttl_seconds: float = BROWSER_BOOTSTRAP_TTL_SECONDS) -> str:
    """Issue a short-lived, single-use capability for opening the model viewer."""
    if ttl_seconds <= 0 or ttl_seconds > BROWSER_BOOTSTRAP_TTL_SECONDS:
        raise ValueError(f"ttl_seconds must be between 0 and {BROWSER_BOOTSTRAP_TTL_SECONDS}")
    nonce = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _browser_auth_lock:
        _prune_expired(_browser_bootstrap_nonces, now)
        _insert_bounded(
            _browser_bootstrap_nonces,
            nonce,
            now + ttl_seconds,
            maximum=_MAX_BROWSER_BOOTSTRAP_NONCES,
        )
    return nonce


def _prune_expired_browser_sessions(now: float) -> None:
    for cookie, (expires_at, _path_token) in tuple(_browser_sessions.items()):
        if expires_at <= now:
            _browser_sessions.pop(cookie, None)


def consume_browser_bootstrap_nonce(nonce: str) -> tuple[str, str] | None:
    """Consume ``nonce`` once and return ``(cookie, random_path_token)``.

    Cookies do not have a port boundary. Scoping the cookie to a fresh,
    unguessable path prevents an unrelated localhost service on another port
    from receiving it under a predictable ``/model`` request.
    """
    if not isinstance(nonce, str) or not 32 <= len(nonce) <= 128:
        return None
    now = time.monotonic()
    with _browser_auth_lock:
        _prune_expired(_browser_bootstrap_nonces, now)
        expires_at = _browser_bootstrap_nonces.pop(nonce, None)
        if expires_at is None or expires_at <= now:
            return None
        session = secrets.token_urlsafe(32)
        path_token = secrets.token_urlsafe(32)
        _prune_expired_browser_sessions(now)
        if len(_browser_sessions) >= _MAX_BROWSER_SESSIONS:
            oldest = min(_browser_sessions, key=lambda key: _browser_sessions[key][0])
            _browser_sessions.pop(oldest, None)
        _browser_sessions[session] = (now + BROWSER_SESSION_TTL_SECONDS, path_token)
        return session, path_token


def _browser_cookie_values(headers: Headers) -> list[str]:
    """Extract exactly named, unquoted cookie values without ambiguous merging."""
    values: list[str] = []
    for header in headers.getlist("cookie"):
        for part in header.split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name == BROWSER_SESSION_COOKIE:
                values.append(value.strip())
    return values


def _browser_session_rewrite(scope: Scope, headers: Headers) -> tuple[str, str] | None:
    """Validate a viewer cookie/path pair and return ``(route_path, base_path)``."""
    path = scope.get("path", "")
    raw_path = scope.get("raw_path")
    if not path.startswith("/model/"):
        return None
    try:
        canonical_raw_path = path.encode("ascii")
    except UnicodeEncodeError:
        return None
    if raw_path is not None and raw_path != canonical_raw_path:
        return None
    remainder = path.removeprefix("/model/")
    path_token, separator, trailing = remainder.partition("/")
    if not 32 <= len(path_token) <= 128:
        return None
    supplied_values = _browser_cookie_values(headers)
    if len(supplied_values) != 1:
        return None
    supplied = supplied_values[0]
    if not 32 <= len(supplied) <= 128:
        return None

    now = time.monotonic()
    matched = False
    with _browser_auth_lock:
        _prune_expired_browser_sessions(now)
        # Do not use a direct dict lookup for the credential.  The bounded
        # store makes a constant-time scan cheap and avoids prefix/timing leaks.
        for expected, (expires_at, expected_path) in _browser_sessions.items():
            cookie_match = hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
            path_match = hmac.compare_digest(
                path_token.encode("utf-8"), expected_path.encode("utf-8")
            )
            matched = bool(matched | (cookie_match and path_match and expires_at > now))
    if not matched:
        return None
    suffix = f"/{trailing}" if separator and trailing else ""
    return f"/model{suffix}", f"/model/{path_token}/"


def reset_browser_auth_state() -> None:
    """Invalidate all viewer capabilities for a fresh HTTP listener generation."""
    with _browser_auth_lock:
        _browser_bootstrap_nonces.clear()
        _browser_sessions.clear()


def _reset_browser_auth_state_for_tests() -> None:
    """Backward-compatible test hook for resetting ephemeral capabilities."""
    reset_browser_auth_state()


class LocalAPIAuthMiddleware:
    """Pure-ASGI bearer/browser authentication that is safe for SSE streaming.

    Only canonical GETs to ``/health`` and the one-time browser bootstrap route
    are public. Model-viewer cookies are accepted only when paired with their
    unguessable per-session path below ``/model``. A missing server credential
    fails closed with 503; invalid credentials return 401. Lifespan events pass
    through unchanged.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope["type"]
        if scope_type not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        if scope_type == "http" and _is_public_http_request(scope):
            await self.app(scope, receive, send)
            return
        # FastMCP wraps the mounted FastAPI app, so HTTP viewer requests cross
        # two instances of this middleware. Only an already-validated outer
        # instance can place this unforgeable in-process sentinel in the scope.
        if scope.get("persome.viewer_authenticated") is _VIEWER_AUTHENTICATED:
            await self.app(scope, receive, send)
            return

        token = local_api_token(required=False)
        if token is None:
            await self._reject(
                scope,
                receive,
                send,
                status_code=503,
                error="local API authentication is not configured",
            )
            return
        headers = Headers(scope=scope)
        browser_rewrite = _browser_session_rewrite(scope, headers) if scope_type == "http" else None
        if not (_has_valid_bearer(headers, token) or browser_rewrite is not None):
            await self._reject(
                scope,
                receive,
                send,
                status_code=401,
                error="unauthorized",
            )
            return

        if browser_rewrite is not None:
            route_path, base_path = browser_rewrite
            scope = dict(scope)
            scope["path"] = route_path
            scope["raw_path"] = route_path.encode("ascii")
            scope["persome.viewer_base_path"] = base_path
            scope["persome.viewer_authenticated"] = _VIEWER_AUTHENTICATED

        async def _send_private(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                if "cache-control" not in response_headers:
                    response_headers["Cache-Control"] = "no-store"
                if "referrer-policy" not in response_headers:
                    response_headers["Referrer-Policy"] = "no-referrer"
            await send(message)

        await self.app(scope, receive, _send_private)

    @staticmethod
    async def _reject(
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status_code: int,
        error: str,
    ) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4401, "reason": error})
            return
        # Authentication rejects before the request body is consumed. Closing
        # the connection prevents a client from drip-feeding that unread body
        # after the response and pinning a keep-alive socket indefinitely.
        headers = {"Cache-Control": "no-store", "Connection": "close"}
        if status_code == 401:
            headers["WWW-Authenticate"] = "Bearer"
        await JSONResponse(
            {"success": False, "error": error},
            status_code=status_code,
            headers=headers,
        )(scope, receive, send)


def add_local_api_auth_middleware(app: Starlette, *, enabled: bool = True) -> Starlette:
    """Attach the shared auth boundary and return ``app`` for easy composition."""
    if enabled:
        app.add_middleware(LocalAPIAuthMiddleware)
    return app
