"""LLM provider registry and runtime profile resolution.

Persome intentionally supports two wire protocols: Anthropic Messages and
OpenAI-compatible Chat Completions. Provider presets supply sensible endpoint,
credential, and model defaults; custom endpoints cover compatible gateways that
are not listed here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal

LLMProtocol = Literal["anthropic", "openai"]


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    protocol: LLMProtocol
    api_key_env: str
    base_url: str
    default_model: str
    description: str
    base_url_env: str = ""
    key_required: bool = True
    local: bool = False
    advanced: bool = False

    @property
    def resolved_base_url_env(self) -> str:
        if self.base_url_env:
            return self.base_url_env
        if self.api_key_env.endswith("_API_KEY"):
            return f"{self.api_key_env[:-8]}_BASE_URL"
        return ""


# These presets are deliberately protocol-level rather than SDK-specific.
# Hosted providers below expose an OpenAI-compatible Chat Completions endpoint;
# Anthropic uses its native Messages endpoint. Local servers must expose /v1.
PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        "anthropic",
        "Anthropic",
        "anthropic",
        "ANTHROPIC_API_KEY",
        "https://api.anthropic.com",
        "claude-sonnet-4-5",
        "Native Anthropic Messages API",
    ),
    ProviderSpec(
        "openai",
        "OpenAI",
        "openai",
        "OPENAI_API_KEY",
        "https://api.openai.com/v1",
        "gpt-4.1-mini",
        "OpenAI Chat Completions API",
    ),
    ProviderSpec(
        "deepseek",
        "DeepSeek",
        "openai",
        "DEEPSEEK_API_KEY",
        "https://api.deepseek.com/v1",
        "deepseek-chat",
        "DeepSeek OpenAI-compatible API",
    ),
    ProviderSpec(
        "openrouter",
        "OpenRouter",
        "openai",
        "OPENROUTER_API_KEY",
        "https://openrouter.ai/api/v1",
        "openai/gpt-4.1-mini",
        "OpenRouter model gateway",
    ),
    ProviderSpec(
        "gemini",
        "Google Gemini",
        "openai",
        "GEMINI_API_KEY",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        "gemini-3.5-flash",
        "Gemini OpenAI compatibility endpoint",
    ),
    ProviderSpec(
        "groq",
        "Groq",
        "openai",
        "GROQ_API_KEY",
        "https://api.groq.com/openai/v1",
        "llama-3.3-70b-versatile",
        "Groq OpenAI-compatible API",
    ),
    ProviderSpec(
        "mistral",
        "Mistral AI",
        "openai",
        "MISTRAL_API_KEY",
        "https://api.mistral.ai/v1",
        "mistral-small-latest",
        "Mistral OpenAI-compatible API",
    ),
    ProviderSpec(
        "xai",
        "xAI",
        "openai",
        "XAI_API_KEY",
        "https://api.x.ai/v1",
        "grok-4.3",
        "xAI OpenAI-compatible API",
    ),
    ProviderSpec(
        "qwen-cn",
        "Qwen (China)",
        "openai",
        "DASHSCOPE_API_KEY",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "qwen-plus",
        "Alibaba Cloud Model Studio China endpoint",
        base_url_env="DASHSCOPE_BASE_URL",
    ),
    ProviderSpec(
        "qwen-us",
        "Qwen (US)",
        "openai",
        "DASHSCOPE_API_KEY",
        "https://dashscope-us.aliyuncs.com/compatible-mode/v1",
        "qwen-plus",
        "Alibaba Cloud Model Studio US endpoint",
        base_url_env="DASHSCOPE_BASE_URL",
    ),
    ProviderSpec(
        "moonshot-cn",
        "Moonshot / Kimi (China)",
        "openai",
        "MOONSHOT_API_KEY",
        "https://api.moonshot.cn/v1",
        "kimi-k2.5",
        "Moonshot China OpenAI-compatible API",
    ),
    ProviderSpec(
        "moonshot-intl",
        "Moonshot / Kimi (International)",
        "openai",
        "MOONSHOT_API_KEY",
        "https://api.moonshot.ai/v1",
        "kimi-k2.5",
        "Moonshot international OpenAI-compatible API",
    ),
    ProviderSpec(
        "zhipu",
        "Zhipu GLM",
        "openai",
        "ZHIPU_API_KEY",
        "https://open.bigmodel.cn/api/paas/v4",
        "glm-4-flash",
        "Zhipu OpenAI-compatible API",
    ),
    ProviderSpec(
        "siliconflow",
        "SiliconFlow",
        "openai",
        "SILICONFLOW_API_KEY",
        "https://api.siliconflow.cn/v1",
        "deepseek-ai/DeepSeek-V3",
        "SiliconFlow OpenAI-compatible API",
    ),
    ProviderSpec(
        "together",
        "Together AI",
        "openai",
        "TOGETHER_API_KEY",
        "https://api.together.xyz/v1",
        "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "Together OpenAI-compatible API",
    ),
    ProviderSpec(
        "fireworks",
        "Fireworks AI",
        "openai",
        "FIREWORKS_API_KEY",
        "https://api.fireworks.ai/inference/v1",
        "accounts/fireworks/models/llama-v3p3-70b-instruct",
        "Fireworks OpenAI-compatible API",
    ),
    ProviderSpec(
        "cerebras",
        "Cerebras",
        "openai",
        "CEREBRAS_API_KEY",
        "https://api.cerebras.ai/v1",
        "llama-3.3-70b",
        "Cerebras OpenAI-compatible API",
    ),
    ProviderSpec(
        "ollama",
        "Ollama (local)",
        "openai",
        "OLLAMA_API_KEY",
        "http://127.0.0.1:11434/v1",
        "qwen3:8b",
        "Local Ollama OpenAI compatibility endpoint",
        key_required=False,
        local=True,
    ),
    ProviderSpec(
        "lm-studio",
        "LM Studio (local)",
        "openai",
        "LM_STUDIO_API_KEY",
        "http://127.0.0.1:1234/v1",
        "local-model",
        "Local LM Studio OpenAI compatibility endpoint",
        key_required=False,
        local=True,
    ),
    ProviderSpec(
        "vllm",
        "vLLM (local)",
        "openai",
        "VLLM_API_KEY",
        "http://127.0.0.1:8000/v1",
        "local-model",
        "Local vLLM OpenAI compatibility endpoint",
        key_required=False,
        local=True,
    ),
    ProviderSpec(
        "azure-openai",
        "Azure OpenAI",
        "openai",
        "AZURE_OPENAI_API_KEY",
        "",
        "deployment-name",
        "Azure OpenAI v1 endpoint; model is the deployment name",
        base_url_env="AZURE_OPENAI_BASE_URL",
        advanced=True,
    ),
    ProviderSpec(
        "custom-openai",
        "Custom OpenAI-compatible",
        "openai",
        "PERSOME_LLM_API_KEY",
        "",
        "model-id",
        "Any OpenAI-compatible Chat Completions endpoint",
        base_url_env="PERSOME_LLM_BASE_URL",
        advanced=True,
    ),
    ProviderSpec(
        "custom-anthropic",
        "Custom Anthropic-compatible",
        "anthropic",
        "PERSOME_LLM_API_KEY",
        "",
        "model-id",
        "Any Anthropic-compatible Messages endpoint",
        base_url_env="PERSOME_LLM_BASE_URL",
        advanced=True,
    ),
)

_BY_ID = {provider.id: provider for provider in PROVIDERS}


@dataclass(frozen=True)
class ResolvedLLMProfile:
    provider: str
    provider_label: str
    protocol: LLMProtocol
    model: str
    base_url: str
    api_key_env: str
    api_key: str | None = field(default=None, repr=False)
    key_required: bool = True
    legacy: bool = False

    @property
    def wire_model(self) -> str:
        """Return the model identifier sent to the selected endpoint.

        Only the selected provider's optional routing prefix is removed. This
        preserves nested model IDs such as ``anthropic/claude-*`` on OpenRouter.
        """
        prefix = f"{self.provider}/"
        if self.model.startswith(prefix):
            return self.model[len(prefix) :]
        return self.model

    @property
    def credential_ready(self) -> bool:
        return bool(self.api_key) or not self.key_required

    def client_api_key(self) -> str:
        """Return an SDK-safe key or raise before any hosted network call."""
        if self.api_key:
            return self.api_key
        if not self.key_required:
            return "persome-local"
        raise RuntimeError(f"{self.api_key_env} is not set for {self.provider_label}")


def provider_spec(provider: str) -> ProviderSpec | None:
    return _BY_ID.get(provider.lower())


def infer_provider(model: str) -> str:
    """Best-effort provider from an optional ``provider/model`` identifier."""
    head = model.split("/", 1)[0].lower() if "/" in model else ""
    if head in _BY_ID:
        return head
    lower = model.lower()
    if lower.startswith("claude"):
        return "anthropic"
    if lower.startswith("deepseek"):
        return "deepseek"
    if lower.startswith("gemini"):
        return "gemini"
    if lower.startswith("grok"):
        return "xai"
    if lower.startswith("mistral") or lower.startswith("ministral"):
        return "mistral"
    if lower.startswith("qwen"):
        return "qwen-cn"
    if lower.startswith("glm"):
        return "zhipu"
    return "openai"


def provider_api_key(provider: str) -> str | None:
    spec = provider_spec(provider)
    return os.environ.get(spec.api_key_env) if spec else None


def provider_base_url(provider: str) -> str | None:
    spec = provider_spec(provider)
    if not spec or not spec.resolved_base_url_env:
        return None
    return os.environ.get(spec.resolved_base_url_env)


def _legacy_anthropic_profile(model_cfg: Any) -> ResolvedLLMProfile | None:
    """Preserve pre-provider Persome installations without rewriting them.

    Before provider profiles existed, every runtime call used ``ANTHROPIC_*``
    regardless of the model name. When no new routing fields are explicit,
    retain those exact semantics, including a missing credential.
    """
    # ``api_key_env`` appeared in older TOMLs while the runtime still ignored
    # it. Only an explicit provider/protocol opts into the new routing contract.
    explicit = any(str(getattr(model_cfg, name, "") or "") for name in ("provider", "protocol"))
    if explicit:
        return None
    key = os.environ.get("ANTHROPIC_API_KEY")
    model = str(getattr(model_cfg, "model", "") or "")
    provider = infer_provider(model)
    spec = provider_spec(provider)
    base_url = str(getattr(model_cfg, "base_url", "") or "")
    base_url = base_url or os.environ.get("ANTHROPIC_BASE_URL", "")
    if not base_url:
        base_url = _BY_ID["anthropic"].base_url
    return ResolvedLLMProfile(
        provider=provider,
        provider_label=f"{spec.label if spec else provider} (legacy Anthropic route)",
        protocol="anthropic",
        model=model,
        base_url=base_url,
        api_key_env="ANTHROPIC_API_KEY",
        api_key=key,
        key_required=True,
        legacy=True,
    )


def resolve_profile(model_cfg: Any) -> ResolvedLLMProfile:
    """Resolve a model/chat config into one complete runtime profile."""
    legacy = _legacy_anthropic_profile(model_cfg)
    if legacy is not None:
        return legacy

    model = str(getattr(model_cfg, "model", "") or "")
    provider = str(getattr(model_cfg, "provider", "") or "") or infer_provider(model)
    spec = provider_spec(provider)
    if spec is None:
        spec = _BY_ID["custom-openai"]
    protocol_raw = str(getattr(model_cfg, "protocol", "") or "") or spec.protocol
    protocol: LLMProtocol = "anthropic" if protocol_raw == "anthropic" else "openai"
    api_key_env = str(getattr(model_cfg, "api_key_env", "") or "") or spec.api_key_env
    base_url = str(getattr(model_cfg, "base_url", "") or "")
    if not base_url and spec.resolved_base_url_env:
        base_url = os.environ.get(spec.resolved_base_url_env, "")
    base_url = base_url or spec.base_url
    return ResolvedLLMProfile(
        provider=provider,
        provider_label=spec.label,
        protocol=protocol,
        model=model or spec.default_model,
        base_url=base_url,
        api_key_env=api_key_env,
        api_key=os.environ.get(api_key_env),
        key_required=spec.key_required,
    )


def make_profile(
    provider: str,
    *,
    model: str,
    base_url: str,
    api_key_env: str,
    api_key: str | None,
    protocol: LLMProtocol | None = None,
) -> ResolvedLLMProfile:
    """Build a profile from onboarding inputs before they are persisted."""
    spec = provider_spec(provider) or _BY_ID["custom-openai"]
    return ResolvedLLMProfile(
        provider=provider,
        provider_label=spec.label,
        protocol=protocol or spec.protocol,
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
        api_key=api_key,
        key_required=spec.key_required,
    )


def detected_providers() -> list[ProviderSpec]:
    """Return hosted provider candidates whose credential is present.

    Region/protocol variants intentionally remain separate even when they use
    the same key variable; guessing the wrong endpoint is worse than prompting.
    """
    return [spec for spec in PROVIDERS if not spec.local and os.environ.get(spec.api_key_env)]
