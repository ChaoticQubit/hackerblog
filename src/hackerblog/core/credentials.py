"""Secrets from the environment and the 0600 secrets.env file (section 18)."""

import os
import stat
from collections.abc import Mapping
from pathlib import Path

from hackerblog.core.errors import ExitCode, HackerBlogError

PROVIDER_KEYS: Mapping[str, str | None] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai_compatible": None,
    "ollama": None,
}


def ensure_private(path: Path, mode_hint: str) -> None:
    """Raise a config error unless path is owned by this user with no group or other access."""
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise HackerBlogError(
            ExitCode.CONFIG,
            f"{path} is accessible by other users",
            f"Run: chmod {mode_hint} {path}",
        )


def read_secrets(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines from path; a missing file yields an empty dict.

    Refuses files other users can read. Blank lines and lines starting with # are skipped,
    and one pair of surrounding quotes is removed from values.
    """
    if not path.exists():
        return {}
    ensure_private(path, "600")
    values: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition("=")
        if not sep or not key.strip().isidentifier():
            raise HackerBlogError(
                ExitCode.CONFIG,
                f"{path}:{number} is not a KEY=VALUE line",
                "Fix or remove that line",
            )
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def lookup(name: str, file_values: Mapping[str, str], env: Mapping[str, str]) -> str | None:
    """Return the secret name from env, else from the file; empty values count as unset."""
    return env.get(name) or file_values.get(name) or None
