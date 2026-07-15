from pathlib import Path

import pytest

from persome import config


def test_defaults_when_no_file(tmp_path: Path) -> None:
    cfg = config.load(tmp_path / "missing.toml")
    assert cfg.capture.interval_minutes == 10
    assert cfg.session.gap_minutes == 5
    assert cfg.reducer.enabled is True
    assert cfg.timeline.max_parallel_windows == 4
    assert cfg.attention_digest_enabled is False
    assert cfg.relation_extraction_enabled is False
    assert cfg.evomem.contradiction_check_enabled is False
    assert cfg.skill_check.max_registered == 20
    assert cfg.skill_check.token_budget == 1000
    default = cfg.model_for("reducer")
    assert default.model == "deepseek-v4-flash"


def test_stage_override_merges(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[models.default]
model = "gpt-5.4-nano"

[models.classifier]
model = "claude-haiku-4-5"
base_url = "https://example/anthropic"
"""
    )
    cfg = config.load(path)
    default = cfg.model_for("default")
    classifier = cfg.model_for("classifier")
    assert default.model == "gpt-5.4-nano"
    assert default.base_url == ""
    assert classifier.model == "claude-haiku-4-5"
    assert classifier.base_url == "https://example/anthropic"


def test_capture_privacy_settings_are_nested(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[capture]
pause_on_lock = false
suppress_secure_input = false
encrypt_screenshots = false
extended_retention_enabled = false
actionable_retention_days = 3
"""
    )
    capture = config.load(path).capture
    assert capture.pause_on_lock is False
    assert capture.suppress_secure_input is False
    assert capture.encrypt_screenshots is False
    assert capture.extended_retention_enabled is False
    assert capture.actionable_retention_days == 3


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source", "typo", "capture.source"),
        ("ocr_policy", "sometimes", "capture.ocr_policy"),
        ("ocr_tier", "huge", "capture.ocr_tier"),
    ],
)
def test_invalid_capture_policy_fails_closed(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f'[capture]\n{field} = "{value}"\n', encoding="utf-8")

    with pytest.raises(RuntimeError, match=message):
        config.load(path)


def test_legacy_top_level_capture_privacy_settings_still_load(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
capture_pause_on_lock = false
capture_encrypt_screenshots = false
capture_actionable_retention_days = 2
"""
    )
    capture = config.load(path).capture
    assert capture.pause_on_lock is False
    assert capture.encrypt_screenshots is False
    assert capture.actionable_retention_days == 2


def test_legacy_route_fields_do_not_silently_change_protocol(tmp_path: Path) -> None:
    """Old inline secrets/routes stay ignored until provider/protocol is explicit."""
    path = tmp_path / "config.toml"
    path.write_text(
        """
[models.default]
model = "gpt-5.4-nano"
api_key_env = "OPENAI_API_KEY"
api_key = "sk-old"

# Stale [chat] table from an install that predates the Chat removal — load()
# must silently ignore it rather than crash.
[chat]
api_key_env = "LEGACY_CHAT_API_KEY"
api_key = "sk-old"
base_url = "https://api.example/anthropic"
"""
    )
    cfg = config.load(path)
    default = cfg.model_for("default")
    assert not hasattr(default, "api_key")
    assert default.api_key_env == "OPENAI_API_KEY"
    assert not hasattr(cfg, "chat")

    from persome.providers import resolve_profile

    assert resolve_profile(default).legacy is True
    assert resolve_profile(default).protocol == "anthropic"


def test_write_default_creates_file(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    assert config.write_default_if_missing(p)
    assert p.exists()
    text = p.read_text()
    assert "[models.default]" in text
    assert "persome llm setup" in text
    assert "PERSOME_LLM_API_KEY" in text
    assert "attention_digest_enabled = false" in text
    assert "relation_extraction_enabled = false" in text
    assert "contradiction_check_enabled = false" in text
    assert "max_registered = 20" in text
    assert "token_budget = 1000" in text
    # idempotent
    assert not config.write_default_if_missing(p)


def test_provider_helpers_read_env(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "k-oai")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openai.example/v1")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    assert config.provider_api_key("openai") == "k-oai"
    assert config.provider_base_url("openai") == "https://openai.example/v1"
    assert config.provider_api_key("deepseek") is None
    assert config.provider_api_key("unknown") is None


def test_infer_provider() -> None:
    assert config.infer_provider("anthropic/claude-haiku-4-5") == "anthropic"
    assert config.infer_provider("openai/gpt-4.1-mini") == "openai"
    assert config.infer_provider("deepseek/deepseek-v4-flash") == "deepseek"
    assert config.infer_provider("claude-haiku-4-5") == "anthropic"
    assert config.infer_provider("deepseek-chat") == "deepseek"
    # Unknown bare names default to openai (litellm's own default).
    assert config.infer_provider("gpt-5.4-nano") == "openai"
    assert config.infer_provider("mystery-model") == "openai"
