"""Locations of the home folder, config, database and secrets."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    """Resolved absolute paths HackerBlog reads and writes."""

    home: Path
    config: Path
    db: Path
    secrets: Path
    logs: Path
    locks: Path


def resolve_paths(
    home: Path | None = None,
    config: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Paths:
    """Resolve paths from explicit arguments, then HACKERBLOG_HOME and HACKERBLOG_CONFIG.

    home defaults to ~/.hackerblog and config to <home>/sites.toml. Empty environment
    values count as unset. Returns absolute paths; nothing is created.
    """
    source = os.environ if env is None else env
    home_value = home or source.get("HACKERBLOG_HOME") or "~/.hackerblog"
    home_path = Path(home_value).expanduser().resolve()
    config_value = config or source.get("HACKERBLOG_CONFIG") or home_path / "sites.toml"
    paths = Paths(
        home=home_path,
        config=Path(config_value).expanduser().resolve(),
        db=home_path / "hackerblog.db",
        secrets=home_path / "secrets.env",
        logs=home_path / "logs",
        locks=home_path / "locks",
    )
    assert paths.db.parent == paths.home
    return paths
