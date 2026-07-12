"""Unified bearer authentication for the daemon-hosted REST and MCP surfaces."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from persome.api import build_api_app
from persome.config import Config
from persome.mcp.server import build_server, endpoint_url
from persome.security.auth import (
    BROWSER_BOOTSTRAP_PATH,
    BROWSER_SESSION_COOKIE,
    LOCAL_API_TOKEN_ENV,
    LocalAPIConfigurationError,
    _reset_browser_auth_state_for_tests,
    auth_headers,
    consume_browser_bootstrap_nonce,
    issue_browser_bootstrap_nonce,
    local_api_token,
    loopback_http_url,
    validate_bind_host,
)
from persome.security.body_limit import DEFAULT_MAX_REQUEST_BODY_BYTES

_TOKEN = "test-local-api-token-with-at-least-32-bytes"


@pytest.fixture(autouse=True)
def _reset_browser_auth_state() -> None:
    _reset_browser_auth_state_for_tests()
    yield
    _reset_browser_auth_state_for_tests()


def test_auth_helpers_read_only_the_local_api_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LOCAL_API_TOKEN_ENV, _TOKEN)

    assert local_api_token() == _TOKEN
    assert auth_headers() == {"Authorization": f"Bearer {_TOKEN}"}


def test_auth_helpers_fail_closed_when_token_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(LOCAL_API_TOKEN_ENV, raising=False)

    assert local_api_token(required=False) is None
    with pytest.raises(LocalAPIConfigurationError, match=LOCAL_API_TOKEN_ENV):
        local_api_token()
    with pytest.raises(LocalAPIConfigurationError, match=LOCAL_API_TOKEN_ENV):
        auth_headers()


def test_loopback_client_url_never_targets_a_remote_or_wildcard_host() -> None:
    assert loopback_http_url("0.0.0.0", 8742, "/mcp") == "http://127.0.0.1:8742/mcp"
    assert loopback_http_url("::", 8742) == "http://127.0.0.1:8742"
    assert loopback_http_url("::1", 8742, "/mcp") == "http://[::1]:8742/mcp"
    with pytest.raises(LocalAPIConfigurationError, match="non-loopback"):
        loopback_http_url("192.0.2.10", 8742, "/mcp")


def test_http_endpoint_url_is_loopback_safe_and_ipv6_correct() -> None:
    cfg = Config()
    cfg.mcp.transport = "streamable-http"
    cfg.mcp.host = "::1"
    assert endpoint_url(cfg) == "http://[::1]:8742/mcp"

    cfg.mcp.host = "192.0.2.10"
    with pytest.raises(LocalAPIConfigurationError, match="non-loopback"):
        endpoint_url(cfg)


@pytest.mark.parametrize("invalid", ["too-short", "x" * 513, " padded-token" + "x" * 32])
def test_auth_helpers_fail_closed_for_weak_or_malformed_tokens(
    invalid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(LOCAL_API_TOKEN_ENV, invalid)

    assert local_api_token(required=False) is None
    with pytest.raises(LocalAPIConfigurationError, match=LOCAL_API_TOKEN_ENV):
        local_api_token()


def test_health_is_public_but_missing_token_closes_protected_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(LOCAL_API_TOKEN_ENV, raising=False)
    client = TestClient(build_api_app(), headers={"host": "127.0.0.1:8742"})

    health = client.get("/health")
    protected = client.get("/openapi.json")

    assert health.status_code == 200
    assert health.json()["data"]["status"] == "ok"
    assert "ocr" in health.json()["data"]
    assert protected.status_code == 503
    assert protected.headers["connection"] == "close"
    assert protected.json() == {
        "success": False,
        "error": "local API authentication is not configured",
    }


def test_unauthorized_rejection_closes_unread_request_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LOCAL_API_TOKEN_ENV, _TOKEN)
    client = TestClient(build_api_app(), headers={"host": "127.0.0.1:8742"})

    response = client.post(
        "/captures/ingest",
        content=b"{}",
        headers={"content-length": str(DEFAULT_MAX_REQUEST_BODY_BYTES + 1)},
    )

    assert response.status_code == 401
    assert response.headers["connection"] == "close"
    assert response.headers["cache-control"] == "no-store"


def test_rest_requires_the_exact_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LOCAL_API_TOKEN_ENV, _TOKEN)
    client = TestClient(build_api_app(), headers={"host": "127.0.0.1:8742"})

    assert client.get("/openapi.json").status_code == 401
    wrong = client.get("/openapi.json", headers={"authorization": "Bearer wrong"})
    assert wrong.status_code == 401
    assert wrong.headers["www-authenticate"] == "Bearer"

    authorized = client.get("/openapi.json", headers=auth_headers())
    assert authorized.status_code == 200
    schema = authorized.json()
    assert schema["components"]["securitySchemes"]["LocalBearer"]["scheme"] == "bearer"
    assert schema["security"] == [{"LocalBearer": []}]
    assert schema["paths"]["/health"]["get"]["security"] == []
    assert authorized.headers["cache-control"] == "no-store"


def test_browser_bootstrap_is_single_use_and_cookie_is_model_only(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(LOCAL_API_TOKEN_ENV, _TOKEN)
    client = TestClient(build_api_app(), headers={"host": "127.0.0.1:8742"})

    assert client.post(BROWSER_BOOTSTRAP_PATH).status_code == 401
    issued = client.post(BROWSER_BOOTSTRAP_PATH, headers=auth_headers())
    assert issued.status_code == 200
    bootstrap_url = issued.json()["data"]["bootstrap_url"]
    assert bootstrap_url.startswith(f"{BROWSER_BOOTSTRAP_PATH}?nonce=")
    assert _TOKEN not in bootstrap_url
    assert issued.headers["cache-control"] == "no-store"

    consumed = client.get(bootstrap_url, follow_redirects=False)
    assert consumed.status_code == 303
    viewer_url = consumed.headers["location"]
    assert viewer_url.startswith("/model/") and viewer_url.endswith("/")
    path_token = viewer_url.removeprefix("/model/").removesuffix("/")
    assert 32 <= len(path_token) <= 128
    set_cookie_raw = consumed.headers["set-cookie"]
    set_cookie = set_cookie_raw.lower()
    assert BROWSER_SESSION_COOKIE in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie
    assert f"path=/model/{path_token}".lower() in set_cookie
    assert set_cookie_raw.split(";", 1)[0].split("=", 1)[1] not in viewer_url

    assert client.get("/model").status_code == 401
    assert client.get(f"/model/{'x' * len(path_token)}/").status_code == 401
    assert client.get(viewer_url).status_code == 200
    viewer_page = client.get(viewer_url)
    assert f'<base href="{viewer_url}">' in viewer_page.text
    assert client.get(viewer_url + "graph").status_code == 200
    assert client.get(viewer_url + "assets/viewer.js").status_code == 200
    assert client.get("/status").status_code == 401

    # Cookie jars ignore ports. The random path prevents a predictable request
    # to an unrelated localhost service from receiving this viewer credential.
    from urllib.request import Request

    predictable = Request("http://testserver:9999/model")
    client.cookies.jar.add_cookie_header(predictable)
    assert BROWSER_SESSION_COOKIE not in (predictable.get_header("Cookie") or "")
    random_path = Request(f"http://testserver:9999{viewer_url}")
    client.cookies.jar.add_cookie_header(random_path)
    assert BROWSER_SESSION_COOKIE in (random_path.get_header("Cookie") or "")

    replay = client.get(bootstrap_url, follow_redirects=False)
    assert replay.status_code == 410


def test_browser_bootstrap_nonce_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LOCAL_API_TOKEN_ENV, _TOKEN)
    now = [100.0]
    monkeypatch.setattr("persome.security.auth.time.monotonic", lambda: now[0])
    client = TestClient(build_api_app(), headers={"host": "127.0.0.1:8742"})

    issued = client.post(BROWSER_BOOTSTRAP_PATH, headers=auth_headers())
    bootstrap_url = issued.json()["data"]["bootstrap_url"]
    now[0] += 61

    assert client.get(bootstrap_url, follow_redirects=False).status_code == 410


def test_new_http_listener_generation_invalidates_browser_cookie(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(LOCAL_API_TOKEN_ENV, _TOKEN)
    client = TestClient(build_api_app(), headers={"host": "127.0.0.1:8742"})
    issued = client.post(BROWSER_BOOTSTRAP_PATH, headers=auth_headers())
    consumed = client.get(issued.json()["data"]["bootstrap_url"], follow_redirects=False)
    assert consumed.status_code == 303
    viewer_url = consumed.headers["location"]
    assert client.get(viewer_url).status_code == 200

    build_server(Config())

    assert client.get(viewer_url).status_code == 401


def test_browser_cookie_crosses_outer_fastmcp_and_inner_api_auth(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(LOCAL_API_TOKEN_ENV, _TOKEN)
    app = build_server(Config()).streamable_http_app()

    with TestClient(app, headers={"host": "127.0.0.1:8742"}) as client:
        issued = client.post(BROWSER_BOOTSTRAP_PATH, headers=auth_headers())
        consumed = client.get(
            issued.json()["data"]["bootstrap_url"],
            follow_redirects=False,
        )

        assert consumed.status_code == 303
        viewer_url = consumed.headers["location"]
        assert client.get(viewer_url).status_code == 200
        assert client.get(viewer_url + "graph").status_code == 200
        assert client.get("/status").status_code == 401


def test_browser_bootstrap_nonce_store_is_bounded() -> None:
    nonces = [issue_browser_bootstrap_nonce() for _ in range(65)]

    assert consume_browser_bootstrap_nonce(nonces[0]) is None
    assert consume_browser_bootstrap_nonce(nonces[-1]) is not None


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/health"),
        ("GET", "/health/"),
        ("GET", "/%68ealth"),
        ("GET", "/auth%2Fbrowser-bootstrap?nonce=" + "x" * 43),
    ],
)
def test_public_path_allowlist_rejects_method_slash_and_encoding_variants(
    method: str,
    path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LOCAL_API_TOKEN_ENV, _TOKEN)
    client = TestClient(build_api_app(), headers={"host": "127.0.0.1:8742"})

    response = client.request(method, path, follow_redirects=False)

    assert response.status_code == 401


def test_duplicate_authorization_headers_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LOCAL_API_TOKEN_ENV, _TOKEN)
    client = TestClient(build_api_app(), headers={"host": "127.0.0.1:8742"})

    response = client.get(
        "/openapi.json",
        headers=[
            ("authorization", f"Bearer {_TOKEN}"),
            ("authorization", f"Bearer {_TOKEN}"),
        ],
    )

    assert response.status_code == 401


def test_auth_can_only_be_disabled_explicitly_for_in_process_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(LOCAL_API_TOKEN_ENV, raising=False)
    client = TestClient(
        build_api_app(auth_enabled=False),
        headers={"host": "127.0.0.1:8742"},
    )

    assert client.get("/openapi.json").status_code == 200


def test_streamable_http_auth_covers_mcp_and_custom_rest_routes(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(LOCAL_API_TOKEN_ENV, _TOKEN)
    server = build_server(Config())
    app = server.streamable_http_app()

    with TestClient(app, headers={"host": "127.0.0.1:8742"}) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/openapi.json").status_code == 401
        assert client.get("/mcp").status_code == 401
        assert client.get("/openapi.json", headers=auth_headers()).status_code == 200
        # An authenticated but protocol-incomplete request reaches the MCP SDK.
        assert client.get("/mcp", headers=auth_headers()).status_code != 401


def test_outer_auth_precedes_fastmcp_body_buffering(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(LOCAL_API_TOKEN_ENV, _TOKEN)
    app = build_server(Config()).streamable_http_app()
    oversized = str(DEFAULT_MAX_REQUEST_BODY_BYTES + 1)

    with TestClient(app, headers={"host": "127.0.0.1:8742"}) as client:
        unauthenticated = client.post(
            "/mcp",
            content=b"{}",
            headers={"content-length": oversized},
        )
        authenticated = client.post(
            "/mcp",
            content=b"{}",
            headers={**auth_headers(), "content-length": oversized},
        )

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 413


def test_sse_outer_app_requires_authentication(ac_root, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LOCAL_API_TOKEN_ENV, _TOKEN)
    cfg = Config()
    cfg.mcp.transport = "sse"
    server = build_server(cfg)

    with TestClient(server.sse_app(), headers={"host": "127.0.0.1:8742"}) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/sse").status_code == 401


def test_non_loopback_bind_is_rejected_even_with_authentication(
    ac_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = Config()
    cfg.mcp.host = "0.0.0.0"
    monkeypatch.delenv(LOCAL_API_TOKEN_ENV, raising=False)

    with pytest.raises(LocalAPIConfigurationError, match="non-loopback"):
        build_server(cfg)

    monkeypatch.setenv(LOCAL_API_TOKEN_ENV, _TOKEN)
    with pytest.raises(LocalAPIConfigurationError, match="non-loopback"):
        build_server(cfg)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.42.0.7", "::1", "localhost"])
def test_loopback_bind_is_valid_without_a_token(host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(LOCAL_API_TOKEN_ENV, raising=False)
    validate_bind_host(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "persome.local"])
def test_non_loopback_bind_is_always_invalid(host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(LOCAL_API_TOKEN_ENV, raising=False)
    with pytest.raises(LocalAPIConfigurationError, match="non-loopback"):
        validate_bind_host(host)
