import io
import json
import logging

import pytest

from hackerblog.core.logging import JsonFormatter, redact, setup_logging

ANTHROPIC_KEY = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
OPENAI_KEY = "sk-proj-1234567890abcdefghijklmnop"
WEBHOOK_SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (f"key={ANTHROPIC_KEY} done", ANTHROPIC_KEY),
        (f"openai {OPENAI_KEY}", OPENAI_KEY),
        (f"secret {WEBHOOK_SECRET}", WEBHOOK_SECRET),
        ("Authorization: Bearer abc.def-ghi_jkl", "abc.def-ghi_jkl"),
        ("ntfy://admin:hunter2@ntfy.example.org/topic", "hunter2"),
    ],
)
def test_redact_removes_secrets(text: str, secret: str) -> None:
    cleaned = redact(text)
    assert secret not in cleaned
    assert "[REDACTED]" in cleaned


def test_redact_keeps_ordinary_text() -> None:
    assert redact("polled 3 sites, 2 new posts") == "polled 3 sites, 2 new posts"


def capture(json_output: bool) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    setup_logging(verbosity=1, json_output=json_output, stream=stream)
    return logging.getLogger("hackerblog.test"), stream


def test_log_args_are_redacted() -> None:
    logger, stream = capture(json_output=False)
    logger.warning("provider rejected key %s", ANTHROPIC_KEY)
    assert ANTHROPIC_KEY not in stream.getvalue()
    assert "provider rejected key [REDACTED]" in stream.getvalue()


def test_exception_text_is_redacted() -> None:
    logger, stream = capture(json_output=False)
    try:
        raise ValueError(f"bad key {OPENAI_KEY}")
    except ValueError:
        logger.exception("call failed")
    assert OPENAI_KEY not in stream.getvalue()


def test_json_formatter_shape_and_redaction() -> None:
    logger, stream = capture(json_output=True)
    logger.info("sent", extra={"site": "s1", "token": WEBHOOK_SECRET})
    record = json.loads(stream.getvalue().splitlines()[-1])
    assert record["level"] == "INFO"
    assert record["msg"] == "sent"
    assert record["site"] == "s1"
    assert WEBHOOK_SECRET not in stream.getvalue()
    assert isinstance(logging.getLogger("hackerblog").handlers[0].formatter, JsonFormatter)
