"""Use cases shared by every adapter. M0 covers `init` and the offline `check`."""

import base64
import json
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from hackerblog.core.config import Config, Problem, load_config
from hackerblog.core.credentials import PROVIDER_KEYS, ensure_private, lookup, read_secrets
from hackerblog.core.errors import ExitCode, HackerBlogError
from hackerblog.core.paths import Paths
from hackerblog.core.store.migrate import connect, migrate

DEFAULT_OUTPUT_DIR = Path("~/HackerBlog")


@dataclass(frozen=True)
class InitResult:
    """What `init` created or found."""

    paths: Paths
    output_dir: Path | None
    wrote_config: bool
    wrote_secrets: bool
    key_name: str | None


@dataclass(frozen=True)
class CheckReport:
    """Outcome of an offline config check."""

    code: ExitCode
    problems: list[Problem] = field(default_factory=list[Problem])
    config: Config | None = None
    missing_key: str | None = None


def starter_config(output_dir: Path, provider: str) -> str:
    """Return the sites.toml text `init` writes: defaults, provider and output dir, no sites."""
    llm = [f"provider = {json.dumps(provider)}"]
    if provider == "anthropic":
        llm += ['notes_model = "claude-haiku-5-5"', 'lesson_model = "claude-sonnet-5-5"']
    return "\n".join(
        [
            "version = 1",
            "",
            "[defaults]",
            'schedule = "daily"',
            "backfill_limit = 25",
            'notify = "digest"',
            "",
            "[llm]",
            *llm,
            "",
            "[output]",
            f"dir = {json.dumps(str(output_dir))}",
            "",
            "[notify]",
            'digest_time = "08:00"',
            "",
        ]
    )


def _starter_secrets(key_name: str | None) -> str:
    webhook = "whsec_" + base64.b64encode(secrets.token_bytes(32)).decode()
    lines = [
        f"HACKERBLOG_API_TOKEN={secrets.token_urlsafe(32)}",
        f"HACKERBLOG_WEBHOOK_SECRET={webhook}",
        "HACKERBLOG_APPRISE_URLS=",
    ]
    if key_name:
        lines.append(f"{key_name}=")
    return "\n".join(lines) + "\n"


def _write_private(path: Path, text: str, overwrite: bool) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    path.chmod(0o600)


def init_home(paths: Paths, output_dir: Path | None, provider: str, force: bool) -> InitResult:
    """Create the home folder, a starter sites.toml, secrets.env and the migrated database.

    An existing config is replaced only when force is true; existing secrets and data are
    never overwritten. Raises a config error if the home folder is open to other users.
    """
    assert provider in PROVIDER_KEYS
    if paths.home.exists():
        ensure_private(paths.home, "700")
    else:
        paths.home.mkdir(mode=0o700, parents=True)
        paths.home.chmod(0o700)
    for folder in (paths.logs, paths.locks):
        folder.mkdir(mode=0o700, exist_ok=True)
    key_name = PROVIDER_KEYS[provider]
    wrote_config = force or not paths.config.exists()
    out = (output_dir or DEFAULT_OUTPUT_DIR).expanduser().resolve() if wrote_config else None
    if out is not None:
        _write_private(paths.config, starter_config(out, provider), overwrite=True)
        out.mkdir(mode=0o700, parents=True, exist_ok=True)
    wrote_secrets = not paths.secrets.exists()
    if wrote_secrets:
        _write_private(paths.secrets, _starter_secrets(key_name), overwrite=False)
    conn = connect(paths.db)
    try:
        migrate(conn)
    finally:
        conn.close()
    paths.db.chmod(0o600)
    return InitResult(paths, out, wrote_config, wrote_secrets, key_name)


def _output_dir_problem(directory: Path) -> Problem | None:
    probe = directory
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if probe.is_dir() and os.access(probe, os.W_OK | os.X_OK):
        return None
    return Problem("output.dir", f"{directory} is not writable", "Pick a writable folder")


def check(paths: Paths, env: Mapping[str, str]) -> CheckReport:
    """Validate config and secrets without network access.

    Exit code CONFIG when the config or secrets file has problems, LLM_AUTH when the
    provider key is missing, otherwise OK.
    """
    config, problems = load_config(paths.config)
    try:
        file_values = read_secrets(paths.secrets)
    except HackerBlogError as error:
        problems.append(Problem("secrets.env", error.message, error.hint))
        file_values = {}
    if config is not None:
        output_problem = _output_dir_problem(config.output.dir)
        if output_problem:
            problems.append(output_problem)
    if problems:
        return CheckReport(ExitCode.CONFIG, problems, config)
    assert config is not None
    key_name = PROVIDER_KEYS[config.llm.provider]
    if key_name and lookup(key_name, file_values, env) is None:
        return CheckReport(ExitCode.LLM_AUTH, [], config, key_name)
    return CheckReport(ExitCode.OK, [], config)
