"""Terminal and JSON output helpers. All untrusted text passes through clean()."""

import json
import re
import sys

from hackerblog.core.errors import ExitCode, HackerBlogError
from hackerblog.core.logging import redact

CONTROL = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]"
)


def clean(text: str) -> str:
    """Strip ANSI escapes, control and bidi characters, and redact secrets."""
    return redact(CONTROL.sub("", text))


def say(text: str = "") -> None:
    """Print one cleaned line to stdout."""
    sys.stdout.write(clean(text) + "\n")


def emit_json(data: object, warnings: list[str] | None = None) -> None:
    """Print the success envelope {"ok": true, "data", "warnings"} to stdout."""
    sys.stdout.write(json.dumps({"ok": True, "data": data, "warnings": warnings or []}) + "\n")


def emit_error(
    error: HackerBlogError, as_json: bool, extra: dict[str, object] | None = None
) -> None:
    """Report an error: JSON envelope on stdout, or `Error:` and `Try:` lines on stderr."""
    assert error.code != ExitCode.OK
    if as_json:
        body: dict[str, object] = {
            "code": error.code.slug,
            "message": clean(error.message),
            "hint": clean(error.hint),
            **(extra or {}),
        }
        sys.stdout.write(json.dumps({"ok": False, "error": body}) + "\n")
        return
    sys.stderr.write(f"Error: {clean(error.message)}\n")
    if error.hint:
        sys.stderr.write(f"Try: {clean(error.hint)}\n")
