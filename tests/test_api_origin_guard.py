"""Origin / Host guard middleware: CSRF-to-localhost + DNS-rebinding hardening.

The API binds 127.0.0.1 only, but that does not stop a browser page from
``fetch()``-ing it (CSRF) or a rebound DNS name from smuggling a non-local
Host. ``_origin_guard`` rejects any request bearing a non-local browser
``Origin`` or a non-local ``Host`` with 403, while letting local same-origin /
native-client requests and ``/health`` through. ``api_require_local_origin``
(default on via ``getattr``) flips it off.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from persome.api import build_api_app
from persome.config import Config


def _local_client(cfg: Config | None = None) -> TestClient:
    """A client whose default Host header is a local one (passes the guard)."""
    return TestClient(build_api_app(cfg), headers={"host": "127.0.0.1:8773"})


def test_browser_origin_is_rejected() -> None:
    """A request carrying a non-local browser Origin → 403."""
    client = _local_client()
    response = client.get("/config", headers={"origin": "https://evil.com"})
    assert response.status_code == 403


def test_non_local_host_is_rejected() -> None:
    """A non-local Host header (DNS-rebinding) → 403, even with no Origin."""
    client = TestClient(build_api_app())
    response = client.get("/config", headers={"host": "attacker.com"})
    assert response.status_code == 403


def test_local_request_passes() -> None:
    """A local request — no Origin, local Host — is NOT blocked by the guard."""
    client = _local_client()
    response = client.get("/config")
    assert response.status_code != 403


def test_local_origin_passes() -> None:
    """A local browser Origin (same-origin XHR) is allowed through."""
    client = _local_client()
    response = client.get("/config", headers={"origin": "http://127.0.0.1:8773"})
    assert response.status_code != 403


def test_null_origin_is_rejected() -> None:
    """``Origin: null`` (sandboxed iframe / file:// / opaque origin) → 403.

    These are attacker-controlled contexts; the guard must NOT allowlist ``null``
    (it has no local host). Native/same-origin callers send no Origin at all.
    """
    client = _local_client()
    response = client.get("/config", headers={"origin": "null"})
    assert response.status_code == 403


def test_localhost_host_passes() -> None:
    """``localhost`` (and its port form) counts as local."""
    client = TestClient(build_api_app(), headers={"host": "localhost:8773"})
    response = client.get("/config")
    assert response.status_code != 403


def test_ipv6_loopback_host_passes() -> None:
    """The bracketed IPv6 loopback ``[::1]`` is treated as local."""
    client = TestClient(build_api_app(), headers={"host": "[::1]:8773"})
    response = client.get("/config")
    assert response.status_code != 403


def test_health_allowed_despite_malicious_origin() -> None:
    """``/health`` is always allowed — a hostile Origin can't trip liveness."""
    client = TestClient(build_api_app())
    response = client.get(
        "/health",
        headers={"origin": "https://evil.com", "host": "attacker.com"},
    )
    assert response.status_code == 200
    assert response.json() == {"success": True, "data": {"status": "ok"}}


def test_disabled_guard_does_not_block() -> None:
    """``api_require_local_origin=False`` → middleware is a no-op."""
    cfg = Config()
    # The field isn't on Config yet (caller adds it later); the middleware reads
    # it via getattr, so an extra attribute exercises the disabled path.
    cfg.api_require_local_origin = False  # type: ignore[attr-defined]
    client = TestClient(build_api_app(cfg))
    response = client.get(
        "/config",
        headers={"origin": "https://evil.com", "host": "attacker.com"},
    )
    assert response.status_code != 403
