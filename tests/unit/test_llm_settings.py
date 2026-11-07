import logging

import pytest

from hackerblog.core.config import LLMConfig
from hackerblog.core.errors import ExitCode, HackerBlogError
from hackerblog.core.llm.settings import LLMSettings

KEY = "sk-ant-api03-SecretValue0123456789abcdef"


def test_from_config_anthropic_defaults() -> None:
    settings = LLMSettings.from_config(LLMConfig(), KEY)
    assert settings.provider == "anthropic"
    assert settings.notes_model == "claude-haiku-5-5"
    assert settings.lesson_model == "claude-sonnet-5-5"
    assert settings.base_url is None
    assert settings.request_timeout_seconds == 120
    assert settings.max_input_tokens == 24_000
    assert settings.api_key is not None
    assert settings.api_key.get_secret_value() == KEY


def test_repr_str_and_logging_never_show_the_key(caplog: pytest.LogCaptureFixture) -> None:
    settings = LLMSettings.from_config(LLMConfig(), KEY)
    with caplog.at_level(logging.INFO):
        logging.getLogger("test").info("settings %s %r", settings, settings)
    assert KEY not in repr(settings)
    assert KEY not in str(settings)
    assert KEY not in caplog.text
    assert "claude-haiku-5-5" in repr(settings)


def test_ollama_needs_no_key_and_gets_v1_url() -> None:
    config = LLMConfig(provider="ollama", notes_model="qwen2.5:7b", lesson_model="qwen2.5:7b")
    settings = LLMSettings.from_config(config, None)
    assert settings.base_url == "http://127.0.0.1:11434/v1"
    assert settings.api_key is None


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_missing_key_for_hosted_provider_is_llm_auth(provider: str) -> None:
    config = LLMConfig.model_validate(
        {"provider": provider, "notes_model": "a", "lesson_model": "b"}
    )
    with pytest.raises(HackerBlogError) as caught:
        LLMSettings.from_config(config, None)
    assert caught.value.code == ExitCode.LLM_AUTH


def test_missing_model_or_base_url_is_config_error() -> None:
    with pytest.raises(HackerBlogError) as caught:
        LLMSettings.from_config(LLMConfig(provider="openai", notes_model="gpt-x"), "sk-1")
    assert caught.value.code == ExitCode.CONFIG
    config = LLMConfig(provider="openai_compatible", notes_model="a", lesson_model="b")
    with pytest.raises(HackerBlogError) as caught:
        LLMSettings.from_config(config, None)
    assert caught.value.code == ExitCode.CONFIG


def test_settings_are_frozen() -> None:
    settings = LLMSettings.from_config(LLMConfig(), KEY)
    name = "notes_model"
    with pytest.raises(AttributeError):
        setattr(settings, name, "other")
