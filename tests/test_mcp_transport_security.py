"""#1 — /mcp transport hardening.

The REST sub-app's Origin/Host guard (api/__init__.py) protects only the mounted REST
routes; `/mcp` lives on the OUTER FastMCP app and never traverses that middleware. So
`build_server` must hand FastMCP a `transport_security` policy that hardens the MCP
transport itself. This test locks the allowlist against the REAL FastMCP middleware: a
native MCP client (local Host, no Origin) passes, while a browser foreign-Origin / rebound
public Host / missing Host is rejected — the CSRF-to-localhost + DNS-rebinding threats.
"""

from __future__ import annotations

from mcp.server.transport_security import TransportSecurityMiddleware

from persome.config import Config
from persome.mcp.server import build_server


def _middleware() -> TransportSecurityMiddleware:
    server = build_server(Config())
    sec = server.settings.transport_security
    assert sec is not None and sec.enable_dns_rebinding_protection is True
    return TransportSecurityMiddleware(sec)


def test_native_local_client_passes() -> None:
    mw = _middleware()
    # Local Host on any port (wildcard) — covers both the default 8742 and the Persome 8773.
    assert mw._validate_host("127.0.0.1:8773") is True
    assert mw._validate_host("localhost:8773") is True
    assert mw._validate_host("127.0.0.1:8742") is True
    # Native MCP clients send no Origin → must pass (else we'd break every local client).
    assert mw._validate_origin(None) is True
    assert mw._validate_origin("http://127.0.0.1:8773") is True


def test_foreign_origin_rejected() -> None:
    mw = _middleware()
    assert mw._validate_origin("http://evil.com") is False
    assert mw._validate_origin("https://attacker.example:8773") is False
    # `Origin: null` (sandboxed iframe / file:// / opaque) must NOT be allowlisted.
    assert mw._validate_origin("null") is False


def test_rebound_or_missing_host_rejected() -> None:
    mw = _middleware()
    assert mw._validate_host("evil.com:8773") is False
    assert mw._validate_host("attacker.example") is False
    assert mw._validate_host(None) is False  # missing Host header
