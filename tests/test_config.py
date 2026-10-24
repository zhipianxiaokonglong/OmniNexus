"""Configuration defaults and privacy-relevant registry behavior."""

from __future__ import annotations

from pathlib import Path

from omninexus.config import load_config

CONFIG = Path(__file__).resolve().parents[1] / "config" / "default.yaml"


def test_default_config_is_reserved_and_local_first() -> None:
    config = load_config(CONFIG)
    assert config.memory.enabled is False
    assert config.memory.is_configured is False
    assert config.search.enabled is False
    assert config.providers["echo"].enabled is False
    assert config.providers["ollama"].enabled is False
    assert len(config.models) == 39
    assert config.model_config_by_id("openai:gpt-4o-mini") is not None


def test_echo_can_be_enabled_for_offline_demo(monkeypatch) -> None:
    monkeypatch.setenv("OMNI_ENABLE_ECHO", "1")
    config = load_config(CONFIG)
    assert config.providers["echo"].enabled is True
    assert any(model.id == "echo:echo" and model.available for model in config.available_models())


def test_no_secret_is_serialized_from_provider_config() -> None:
    config = load_config(CONFIG)
    dumped = config.model_dump_json()
    assert "test-secret" not in dumped
    assert "api_key_env" in dumped  # names are public; values are not present


def test_bundled_default_config_matches_repository_default() -> None:
    bundled = (
        Path(__file__).resolve().parents[1] / "src" / "omninexus" / "resources" / "default.yaml"
    )
    assert bundled.read_text(encoding="utf-8") == CONFIG.read_text(encoding="utf-8")
