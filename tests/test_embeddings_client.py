"""embeddings_client endpoint/auth resolution — relay vs Azure (offline, no network)."""

from __future__ import annotations

import pytest

from persome.writer import embeddings_client as ec


@pytest.mark.parametrize(
    "base, want_url, want_header",
    [
        # relay: append /embeddings, JWT in x-api-key
        (
            "https://persome-web.vercel.app/api/llm",
            "https://persome-web.vercel.app/api/llm/embeddings",
            "x-api-key",
        ),
        ("https://web/api/llm/", "https://web/api/llm/embeddings", "x-api-key"),
        # Azure OpenAI: full route used verbatim, key in api-key
        (
            "https://persome-resource.cognitiveservices.azure.com/openai/deployments/text-embedding-3-large/embeddings?api-version=2023-05-15",
            "https://persome-resource.cognitiveservices.azure.com/openai/deployments/text-embedding-3-large/embeddings?api-version=2023-05-15",
            "api-key",
        ),
        # a plain full /embeddings URL (non-Azure) keeps x-api-key
        ("https://host/v1/embeddings", "https://host/v1/embeddings", "x-api-key"),
    ],
)
def test_resolve_endpoint(monkeypatch, base, want_url, want_header):
    monkeypatch.setattr(ec, "provider_base_url", lambda _p: base)
    url, header = ec._resolve_endpoint()
    assert url == want_url
    assert header == want_header


def test_resolve_endpoint_unconfigured(monkeypatch):
    monkeypatch.setattr(ec, "provider_base_url", lambda _p: None)
    assert ec._resolve_endpoint() is None
