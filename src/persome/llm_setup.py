"""Guided, test-before-save LLM provider onboarding."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import env_file, paths
from .providers import LLM_API_KEY_ENV, ResolvedLLMProfile

_PROBE_TOOL_NAME = "persome_setup_check"
_PROBE_TOOL_DESCRIPTION = "Confirm that this model can call Persome memory tools."
_PROBE_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


@dataclass(frozen=True)
class ProbeResult:
    completion_ok: bool
    tool_call_ok: bool
    latency_ms: int | None
    error: str | None = None


def _safe_error(exc: Exception, api_key: str | None) -> str:
    message = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    if api_key:
        message = message.replace(api_key, "***")
    label = type(exc).__name__
    return f"{label}: {message[:180]}" if message else label


def probe_profile(profile: ResolvedLLMProfile, *, timeout: float = 20.0) -> ProbeResult:
    """Verify authentication/completion, then test required tool calling.

    A successful completion is the hard connectivity gate. Tool calling is
    reported separately because some compatible models authenticate correctly
    but cannot run Persome's modeling tool loops.
    """
    started = time.monotonic()
    try:
        if profile.protocol == "anthropic":
            import anthropic

            client = anthropic.Anthropic(
                api_key=profile.client_api_key(),
                base_url=profile.base_url or None,
                timeout=timeout,
            )
            client.messages.create(
                model=profile.wire_model,
                messages=[{"role": "user", "content": "Reply with exactly: ok"}],
                max_tokens=8,
            )
        else:
            from openai import OpenAI

            client = OpenAI(
                api_key=profile.client_api_key(),
                base_url=profile.base_url,
                timeout=timeout,
            )
            client.chat.completions.create(
                model=profile.wire_model,
                messages=[{"role": "user", "content": "Reply with exactly: ok"}],
                max_tokens=8,
            )
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            completion_ok=False,
            tool_call_ok=False,
            latency_ms=None,
            error=_safe_error(exc, profile.api_key),
        )

    tool_ok = False
    tool_error: str | None = None
    prompt = f"You must call {_PROBE_TOOL_NAME} now. Do not answer with text."
    if profile.protocol == "anthropic":
        tool = {
            "name": _PROBE_TOOL_NAME,
            "description": _PROBE_TOOL_DESCRIPTION,
            "input_schema": _PROBE_SCHEMA,
        }
        choices: list[Any] = [
            {"type": "tool", "name": _PROBE_TOOL_NAME},
            {"type": "auto"},
        ]
        for tool_choice in choices:
            try:
                tool_response = client.messages.create(
                    model=profile.wire_model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=128,
                    tools=[tool],
                    tool_choice=tool_choice,
                )
            except Exception as exc:  # noqa: BLE001
                tool_error = _safe_error(exc, profile.api_key)
                continue
            tool_ok = any(
                getattr(block, "type", None) == "tool_use"
                and getattr(block, "name", None) == _PROBE_TOOL_NAME
                for block in tool_response.content
            )
            if tool_ok:
                break
    else:
        tool = {
            "type": "function",
            "function": {
                "name": _PROBE_TOOL_NAME,
                "description": _PROBE_TOOL_DESCRIPTION,
                "parameters": _PROBE_SCHEMA,
            },
        }
        choices = [
            {"type": "function", "function": {"name": _PROBE_TOOL_NAME}},
            "auto",
        ]
        for tool_choice in choices:
            try:
                tool_response = client.chat.completions.create(
                    model=profile.wire_model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=128,
                    tools=[tool],
                    tool_choice=tool_choice,
                )
            except Exception as exc:  # noqa: BLE001
                tool_error = _safe_error(exc, profile.api_key)
                continue
            tool_ok = any(
                call.function.name == _PROBE_TOOL_NAME
                for call in (tool_response.choices[0].message.tool_calls or [])
            )
            if tool_ok:
                break
    return ProbeResult(
        completion_ok=True,
        tool_call_ok=tool_ok,
        latency_ms=int((time.monotonic() - started) * 1000),
        error=None if tool_ok else tool_error,
    )


def save_profile(
    profile: ResolvedLLMProfile,
    *,
    config_path: Path,
    env_path: Path,
) -> None:
    """Persist one verified profile without placing its secret in TOML."""
    import tomlkit
    from tomlkit.items import Table

    if config_path.exists():
        document = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    else:
        document = tomlkit.document()

    models = document.get("models")
    if not isinstance(models, Table):
        models = tomlkit.table()
        document["models"] = models
    default = models.get("default")
    if not isinstance(default, Table):
        default = tomlkit.table()
        models["default"] = default

    managed_values = {
        "provider": profile.provider,
        "protocol": profile.protocol,
        "model": profile.model,
        "base_url": profile.base_url,
        "api_key_env": LLM_API_KEY_ENV,
    }
    for key, value in managed_values.items():
        default[key] = value
        # These fields are owned by onboarding. Drop stale provider-specific
        # inline comments retained by tomlkit when an existing value is replaced.
        item = default.item(key)
        item.trivia.comment_ws = ""
        item.trivia.comment = ""

    if config_path.is_symlink():
        raise RuntimeError(f"config file must not be a symlink: {config_path}")
    if profile.api_key:
        env_file.write_env_values(env_path, {LLM_API_KEY_ENV: profile.api_key})
    paths.atomic_write_private_text(config_path, tomlkit.dumps(document))


def profile_dict(profile: ResolvedLLMProfile) -> dict[str, Any]:
    """Public, secret-free profile fields for CLI/API status surfaces."""
    return {
        "provider": profile.provider,
        "provider_label": profile.provider_label,
        "protocol": profile.protocol,
        "model": profile.model,
        "base_url": profile.base_url,
        "api_key_env": profile.api_key_env,
        "credential_ready": profile.credential_ready,
        "legacy": profile.legacy,
    }
