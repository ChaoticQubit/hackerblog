"""Logging setup with secret redaction and a JSON line formatter."""

import json
import logging
import re
import sys
from typing import TextIO

STANDARD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "taskName",
}


REDACTED = "[REDACTED]"
SECRET_PATTERNS = (
    (re.compile(r"sk-[A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"whsec_[A-Za-z0-9+/=_-]{8,}"), REDACTED),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1" + REDACTED),
    (re.compile(r"([a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+@"), r"\1" + REDACTED + "@"),
)


def redact(text: str) -> str:
    """Replace provider keys, webhook secrets, bearer tokens and URL passwords in text."""
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    """Redact secrets from a record's message and string extras before any handler sees them."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        for key, value in vars(record).items():
            if key not in STANDARD_ATTRS and isinstance(value, str):
                setattr(record, key, redact(value))
        return True


class HumanFormatter(logging.Formatter):
    """Format records as `level message` for terminals, redacting the final text."""

    def __init__(self) -> None:
        super().__init__("%(levelname)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class JsonFormatter(logging.Formatter):
    """Format records as one JSON object per line with ts, level, event, msg and extras."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "event": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in STANDARD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, default=str))


def setup_logging(verbosity: int, json_output: bool = False, stream: TextIO | None = None) -> None:
    """Configure the `hackerblog` logger.

    verbosity 0 logs warnings, 1 info, 2 or more debug. Output goes to stream (stderr by
    default), as JSON lines when json_output is true. Replaces handlers from earlier calls.
    """
    assert verbosity >= 0
    logger = logging.getLogger("hackerblog")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(JsonFormatter() if json_output else HumanFormatter())
    logger.addHandler(handler)
    logger.setLevel([logging.WARNING, logging.INFO, logging.DEBUG][min(verbosity, 2)])
    logger.propagate = False
