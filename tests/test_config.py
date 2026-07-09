from pathlib import Path

from persome import config


def test_defaults_when_no_file(tmp_path: Path) -> None:
    cfg = config.load(tmp_path / "missing.toml")
    assert cfg.capture.interval_minutes == 10
    assert cfg.session.gap_minutes == 5
    assert cfg.reducer.enabled is True
    assert cfg.timeline.max_parallel_windows == 4
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


def test_legacy_api_key_fields_ignored(tmp_path: Path) -> None:
    """Old TOMLs may still set ``api_key`` / ``api_key_env`` — silently drop
    them so users can upgrade without a migration step."""
    path = tmp_path / "config.toml"
    path.write_text(
        """
[models.default]
model = "gpt-5.4-nano"
api_key_env = "OPENAI_API_KEY"
api_key = "sk-old"

[chat]
api_key_env = "ANTHROPIC_API_KEY"
api_key = "sk-old"
base_url = "https://api.example/anthropic"
"""
    )
    cfg = config.load(path)
    default = cfg.model_for("default")
    # No AttributeError, no leakage into the dataclass.
    assert not hasattr(default, "api_key")
    assert not hasattr(default, "api_key_env")
    assert not hasattr(cfg.chat, "api_key")
    assert not hasattr(cfg.chat, "api_key_env")
    assert not hasattr(cfg.chat, "base_url")


def test_write_default_creates_file(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    assert config.write_default_if_missing(p)
    assert p.exists()
    text = p.read_text()
    assert "[models.default]" in text
    # Template no longer mentions key/env fields.
    assert "api_key_env" not in text
    # idempotent
    assert not config.write_default_if_missing(p)


def test_provider_helpers_read_env(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k-ant")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://ant")
    monkeypatch.setenv("OPENAI_API_KEY", "k-oai")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    assert config.provider_api_key("anthropic") == "k-ant"
    assert config.provider_base_url("anthropic") == "https://ant"
    assert config.provider_api_key("openai") == "k-oai"
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


def test_debug_hud_defaults_to_intent_only(tmp_path: Path) -> None:
    cfg = config.load(tmp_path / "missing.toml")
    assert cfg.debug_hud.show == ["intent"]


def test_debug_hud_show_override(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[debug_hud]\nshow = ["intent", "tool_call", "health"]\n')
    cfg = config.load(path)
    assert cfg.debug_hud.show == ["intent", "tool_call", "health"]


def test_set_debug_hud_show_replaces_existing_line() -> None:
    text = '[capture]\nx = 1\n\n[debug_hud]\nshow = ["intent"]\n'
    out = config.set_debug_hud_show(text, ["intent", "health"])
    assert 'show = ["intent", "health"]' in out
    assert "[capture]" in out and "x = 1" in out  # untouched
    assert out.count("[debug_hud]") == 1


def test_set_debug_hud_show_appends_section_when_absent() -> None:
    text = "[capture]\nx = 1\n"
    out = config.set_debug_hud_show(text, ["intent", "tool_call"])
    assert "[debug_hud]" in out
    assert 'show = ["intent", "tool_call"]' in out
    assert config.load.__module__  # sanity
    import tomllib

    assert tomllib.loads(out)["debug_hud"]["show"] == ["intent", "tool_call"]


def test_set_debug_hud_show_inserts_when_section_has_no_show() -> None:
    text = "[debug_hud]\n# nothing yet\n"
    out = config.set_debug_hud_show(text, ["memory"])
    import tomllib

    assert tomllib.loads(out)["debug_hud"]["show"] == ["memory"]


def test_top_level_flat_flags_load_from_toml(tmp_path: Path) -> None:
    """Reverse-loop G1 regression: the flat top-level feature flags must actually be
    READ from config.toml by ``load()`` — not just exist on the dataclass. The G1
    daemon kill-switch ``memory_ingest_enabled`` was added to the ``Config`` dataclass
    but its ``raw.get`` line was missing from ``load()``, so the flag was stuck at its
    ``False`` default and the channel could never be enabled (a real-E2E-caught bug).
    Pin a representative sibling too so the wiring can't silently rot again."""
    # default (no file) → the dataclass default
    assert config.load(tmp_path / "missing.toml").memory_ingest_enabled is False
    # explicit top-level override IS honoured
    p = tmp_path / "config.toml"
    p.write_text("memory_ingest_enabled = true\nrewind_enabled = false\n")
    cfg = config.load(p)
    assert cfg.memory_ingest_enabled is True
    assert cfg.rewind_enabled is False
