"""Settings for tests against a real model: Anthropic when a key is set, else local Ollama."""

import json
import os
from http.client import HTTPConnection
from urllib.parse import urlsplit

import pytest
from pydantic import SecretStr

from hackerblog.core.llm.settings import LLMSettings

OLLAMA_URL = os.environ.get("HACKERBLOG_TEST_OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("HACKERBLOG_TEST_OLLAMA_MODEL", "qwen2.5:7b")


def ollama_has_model() -> bool:
    parts = urlsplit(OLLAMA_URL)
    connection = HTTPConnection(parts.hostname or "127.0.0.1", parts.port or 80, timeout=3)
    try:
        connection.request("GET", "/api/tags")
        tags = json.load(connection.getresponse())
    except OSError:
        return False
    finally:
        connection.close()
    return any(model.get("name") == OLLAMA_MODEL for model in tags.get("models", []))


def live_settings() -> LLMSettings:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return LLMSettings(
            provider="anthropic",
            notes_model="claude-haiku-5-5",
            lesson_model="claude-sonnet-5-5",
            base_url=None,
            api_key=SecretStr(key),
            request_timeout_seconds=600,
            max_input_tokens=24_000,
        )
    if not ollama_has_model():
        pytest.skip(f"no ANTHROPIC_API_KEY and Ollama at {OLLAMA_URL} lacks {OLLAMA_MODEL}")
    return LLMSettings(
        provider="ollama",
        notes_model=OLLAMA_MODEL,
        lesson_model=OLLAMA_MODEL,
        base_url=f"{OLLAMA_URL}/v1",
        api_key=None,
        request_timeout_seconds=600,
        max_input_tokens=24_000,
    )
