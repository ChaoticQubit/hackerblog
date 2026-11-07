"""Runtime LLM settings built from the [llm] config table and the provider key (section 18)."""

from dataclasses import dataclass, field
from typing import Literal, Self

from pydantic import SecretStr

from hackerblog.core.config import LLMConfig
from hackerblog.core.credentials import PROVIDER_KEYS
from hackerblog.core.errors import ExitCode, HackerBlogError

Provider = Literal["anthropic", "openai", "openai_compatible", "ollama"]


@dataclass(frozen=True)
class LLMSettings:
    """Everything the backend needs for one call. The key is a SecretStr hidden from repr."""

    provider: Provider
    notes_model: str
    lesson_model: str
    base_url: str | None
    api_key: SecretStr | None = field(repr=False)
    request_timeout_seconds: int
    max_input_tokens: int

    def __post_init__(self) -> None:
        assert self.notes_model
        assert self.lesson_model
        assert self.request_timeout_seconds > 0
        assert self.max_input_tokens > 0

    @classmethod
    def from_config(cls, llm_config: LLMConfig, api_key: str | None) -> Self:
        """Build settings from a validated [llm] table and the provider key, if any.

        Raises HackerBlogError CONFIG for a missing model or base_url, and LLM_AUTH when a
        hosted provider has no key.
        """
        provider = llm_config.provider
        notes_model, lesson_model = llm_config.notes_model, llm_config.lesson_model
        if notes_model is None or lesson_model is None:
            raise HackerBlogError(
                ExitCode.CONFIG,
                f"notes_model and lesson_model are required for provider {provider}",
                "Set both in [llm] of sites.toml",
            )
        if provider in {"openai_compatible", "ollama"} and llm_config.base_url is None:
            raise HackerBlogError(
                ExitCode.CONFIG, f"base_url is required for {provider}", "Set base_url in [llm]"
            )
        key_name = PROVIDER_KEYS[provider]
        if key_name is not None and not api_key:
            raise HackerBlogError(
                ExitCode.LLM_AUTH, f"{key_name} is not set", f"Add {key_name} to secrets.env"
            )
        settings = cls(
            provider=provider,
            notes_model=notes_model,
            lesson_model=lesson_model,
            base_url=llm_config.base_url,
            api_key=SecretStr(api_key) if api_key else None,
            request_timeout_seconds=llm_config.request_timeout_seconds,
            max_input_tokens=llm_config.max_input_tokens,
        )
        assert settings.provider == llm_config.provider
        assert (settings.api_key is None) == (not api_key)
        return settings
