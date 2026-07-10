"""Embeddings via the Persome relay (text-embedding-3-large), authenticated by the user's JWT.

Provider-backed embeddings for optional hybrid retrieval.

No bundled key — mirrors the LLM path: the daemon reads ``OPENAI_API_KEY`` (= the user's JWT)
and ``OPENAI_BASE_URL`` (= the relay's OpenAI-compatible base) from ``~/.persome/chronicle/env``
(``config.provider_*("openai")``) and POSTs to ``{base}/embeddings`` with the JWT in
``x-api-key``. The relay route ``/api/llm/embeddings`` (persome-server, separate repo) proxies to
OpenAI and meters ``usageKind="embedding"``.

**Fail-open everywhere**: not configured / relay down / bad response → return ``None`` (per
text). Callers degrade to BM25-only — the dense layer is an additive enhancement, never a
dependency.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from ..config import provider_api_key, provider_base_url
from ..logger import get

_log = get("persome.embeddings_client")


@lru_cache(maxsize=1)
def _http_client() -> Any:
    """Cached keep-alive HTTP client. urllib opened a fresh TCP+TLS connection on EVERY embed call —
    several handshake RTTs to a far (Azure) endpoint per call. A reused httpx client pools the
    connection, so warm calls skip the handshake. Thread-safe (httpx.Client) for the fast-path pool.
    Pure latency, identical requests — zero effect on the vectors produced."""
    import httpx  # lazy — keep CLI startup fast

    return httpx.Client(timeout=_TIMEOUT)


MODEL = "text-embedding-3-large"
_TIMEOUT = 60.0
_RETRIES = 3
_MAX_CHARS = 6000  # te3-large ctx = 8191 TOKENS. CJK tokenizes to >1 token/char, so the old
# 24000-char clip let oversized Chinese through → HTTP 400 → silent None (entry stayed
# BM25-only / dense query degraded with no log beyond a 400 line). 6000 chars stays under 8191
# tokens even for dense CJK (binary-searched: 6000 OK, 7000 → 400). A single memory entry / a
# focused query is far shorter, so English loses only headroom it never used. Inputs are
# head-clipped here; the dense RECALL query is TAIL-clipped upstream (recent = relevant).


def available() -> bool:
    """True iff both the relay base and the JWT are present (env-configured)."""
    return bool(provider_api_key("openai") and provider_base_url("openai"))


def _resolve_endpoint() -> tuple[str, str] | None:
    """(url, auth_header_name) for the embeddings POST, or None if unconfigured.

    Supports two shapes via ``OPENAI_BASE_URL``:
    - **Relay** (default): a base like ``https://web/api/llm`` → ``{base}/embeddings`` with the
      JWT in ``x-api-key`` (the Persome relay convention).
    - **Azure OpenAI / full URL**: a base that already names the route
      (``…/openai/deployments/<dep>/embeddings?api-version=…``, or any URL ending in
      ``/embeddings`` / carrying ``api-version=``) is used VERBATIM, with the key in ``api-key``
      (Azure's header). Lets a bring-your-own-key user point straight at Azure te3-large.
    """
    base = provider_base_url("openai")
    if not base:
        return None
    low = base.lower()
    is_full = (
        "/deployments/" in low or "api-version=" in low or low.rstrip("/").endswith("/embeddings")
    )
    if is_full:
        header = "api-key" if ("azure" in low or "cognitiveservices" in low) else "x-api-key"
        return base, header
    return base.rstrip("/") + "/embeddings", "x-api-key"


def embed_batch(texts: list[str], *, model: str = MODEL) -> list[list[float] | None]:
    """Embed a batch. Returns one vector per input (``None`` for any that failed — never
    raises). An all-fail batch (not configured / relay error) returns ``[None, …]`` so the
    caller leaves those entries queued and BM25-only."""
    if not texts:
        return []
    key = provider_api_key("openai")
    resolved = _resolve_endpoint()
    if not key or not resolved:
        return [None] * len(texts)
    endpoint, auth_header = resolved
    clipped = [(t or "")[:_MAX_CHARS] for t in texts]
    body = json.dumps({"model": model, "input": clipped}).encode()
    headers = {auth_header: key, "Content-Type": "application/json"}
    last: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            resp = _http_client().post(endpoint, content=body, headers=headers)
            if resp.status_code != 429 and 400 <= resp.status_code < 500:
                _log.warning("embeddings permanent HTTP %s — degrade to BM25", resp.status_code)
                return [None] * len(texts)
            if resp.status_code >= 300:
                raise RuntimeError(f"embeddings HTTP {resp.status_code}")
            d = resp.json()
            data = d.get("data")
            if not isinstance(data, list) or len(data) != len(clipped):
                raise ValueError(f"embeddings response shape mismatch: {str(d)[:160]}")
            ordered = sorted(data, key=lambda x: x.get("index", 0))
            return [it.get("embedding") for it in ordered]
        except Exception as e:  # noqa: BLE001 — fail-open: any error → BM25 fallback
            last = e
        if attempt < _RETRIES - 1:
            import time  # noqa: PLC0415

            time.sleep(1.0 * (attempt + 1))
    _log.warning("embeddings failed after %d tries (%r) — degrade to BM25", _RETRIES, last)
    return [None] * len(texts)


def embed(text: str, *, model: str = MODEL) -> list[float] | None:
    """Embed one string (query-time). ``None`` on any failure → caller falls back to BM25."""
    return embed_batch([text], model=model)[0]
