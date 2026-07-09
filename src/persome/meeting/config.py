"""Meeting analysis configuration — LLM + trigger settings for the analysis server."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LLMConfig:
    model: str = "deepseek/deepseek-v4-flash"
    api_key_env: str = "DEEPSEEK_API_KEY"
    base_url: str = "https://api.deepseek.com"
    max_tokens: int = 512


@dataclass
class TriggerConfig:
    pause_seconds: float = 0.5
    max_interval_seconds: float = 3.0
    context_window_seconds: float = 20.0


@dataclass
class MeetingConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    trigger: TriggerConfig = field(default_factory=TriggerConfig)
    db_path: str = ""
