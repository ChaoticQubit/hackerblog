"""Exit codes and the error type every adapter maps to a CLI exit code or an HTTP problem."""

from enum import IntEnum


class ExitCode(IntEnum):
    """Process exit codes from docs/architecture.md section 15.2."""

    OK = 0
    INTERNAL = 1
    USAGE = 2
    CONFIG = 3
    NOT_FOUND = 4
    PARTIAL = 5
    FAILED = 6
    BUDGET = 7
    LOCKED = 8
    BLOCKED = 9
    LLM_AUTH = 10
    INTERRUPTED = 130

    @property
    def slug(self) -> str:
        """Lowercase name used as the `code` field in JSON errors."""
        return self.name.lower()


class HackerBlogError(Exception):
    """An expected failure with an exit code, a message and the next command to try."""

    def __init__(self, code: ExitCode, message: str, hint: str = "") -> None:
        assert code != ExitCode.OK
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
