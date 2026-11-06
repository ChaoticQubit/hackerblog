"""sites.toml models, loading and validation (docs/architecture.md section 7)."""

import re
import tomllib
import zoneinfo
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from hackerblog.core.net.urls import url_policy_problem

ANTHROPIC_NOTES_MODEL = "claude-haiku-5-5"
ANTHROPIC_LESSON_MODEL = "claude-sonnet-5-5"
OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1"
SCHEDULE_WORDS = frozenset({"hourly", "daily", "weekly"})
CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
CRON_FIELD = re.compile(r"^(\*|(\d+)(?:-(\d+))?)(?:/(\d+))?$")

Notify = Literal["instant", "digest", "off"]
Slug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,47}$")]


@dataclass(frozen=True)
class Problem:
    """One config problem: where it is, what is wrong and how to fix it."""

    location: str
    message: str
    hint: str
    line: int | None = None

    def render(self, file_name: str) -> str:
        """Format as `file:line location: message. hint` for terminal output."""
        where = f"{file_name}:{self.line}" if self.line else file_name
        return f"{where} {self.location}: {self.message}. {self.hint}".rstrip()


def validate_cron(expr: str) -> str | None:
    """Return why a 5-field numeric cron expression is invalid, or None when it is valid."""
    fields = expr.split()
    if len(fields) != 5:
        return "cron expression needs 5 fields"
    for field, (low, high) in zip(fields, CRON_RANGES, strict=True):
        for part in field.split(","):
            match = CRON_FIELD.match(part)
            if match is None:
                return f"cron field {field!r} is not numeric (*, n, n-m, */s, n-m/s)"
            start, end, step = match.group(2), match.group(3), match.group(4)
            if step is not None and int(step) == 0:
                return f"cron step in {field!r} must be positive"
            if start is None:
                continue
            first, last = int(start), int(end or start)
            if not low <= first <= last <= high:
                return f"cron field {field!r} must be within {low}-{high}"
    return None


def _check_schedule(value: str) -> str:
    if value in SCHEDULE_WORDS:
        return value
    problem = validate_cron(value)
    if problem:
        raise ValueError(f"{problem}; use hourly, daily, weekly or a 5-field cron expression")
    return value


def _check_regex(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        re.compile(value)
    except re.error as error:
        raise ValueError(f"invalid regex: {error}") from error
    return value


class StrictModel(BaseModel):
    """Base model that rejects unknown keys."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Defaults(StrictModel):
    """Values every site inherits unless it overrides them."""

    schedule: str = "daily"
    backfill_limit: int | None = Field(default=None, ge=0)
    notify: Notify = "digest"
    max_new_per_poll: int = Field(default=20, ge=1, le=200)
    enabled: bool = True

    _schedule = field_validator("schedule")(_check_schedule)


class SiteConfig(StrictModel):
    """One followed blog. Override fields left as None inherit from [defaults]."""

    url: str = Field(max_length=2048)
    name: str | None = Field(default=None, min_length=1, max_length=80)
    slug: Slug | None = None
    feed: str | None = Field(default=None, max_length=2048)
    sitemap: str | None = Field(default=None, max_length=2048)
    post_url_pattern: str | None = Field(default=None, max_length=200)
    tags: list[str] = Field(default_factory=list[str], max_length=20)
    schedule: str | None = None
    backfill_limit: int | None = Field(default=None, ge=0)
    notify: Notify | None = None
    max_new_per_poll: int | None = Field(default=None, ge=1, le=200)
    enabled: bool | None = None

    _pattern = field_validator("post_url_pattern")(_check_regex)

    @field_validator("schedule")
    @classmethod
    def _schedule(cls, value: str | None) -> str | None:
        return None if value is None else _check_schedule(value)

    @model_validator(mode="after")
    def _fill_slug(self) -> "SiteConfig":
        if self.slug is None:
            self.slug = derive_slug(self.url)
        return self


class LLMConfig(StrictModel):
    """Provider and model settings."""

    provider: Literal["anthropic", "openai", "openai_compatible", "ollama"] = "anthropic"
    notes_model: str | None = Field(default=None, min_length=1, max_length=200)
    lesson_model: str | None = Field(default=None, min_length=1, max_length=200)
    base_url: str | None = Field(default=None, max_length=2048)
    daily_token_budget: int | None = Field(default=None, ge=1)
    max_input_tokens: int = Field(default=24_000, ge=1000)
    request_timeout_seconds: int = Field(default=120, ge=1, le=3600)

    @model_validator(mode="after")
    def _provider_defaults(self) -> "LLMConfig":
        if self.provider == "anthropic":
            self.notes_model = self.notes_model or ANTHROPIC_NOTES_MODEL
            self.lesson_model = self.lesson_model or ANTHROPIC_LESSON_MODEL
        if self.provider == "ollama":
            self.base_url = self.base_url or OLLAMA_BASE_URL
        return self


class OutputConfig(StrictModel):
    """Where generated files go."""

    dir: Path = Path("~/HackerBlog")
    public_base_url: str | None = Field(default=None, max_length=2048)

    @field_validator("dir")
    @classmethod
    def _expand(cls, value: Path) -> Path:
        return value.expanduser()


class NotifyConfig(StrictModel):
    """Delivery settings. Secrets for these come from the environment, never this file."""

    digest_time: str = Field(default="08:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    timezone: str | None = None
    webhook_url: str | None = Field(default=None, max_length=2048)
    webhook_allow_private: bool = False
    unhealthy_after_failures: int = Field(default=3, ge=1, le=100)

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            zoneinfo.ZoneInfo(value)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"unknown IANA time zone {value!r}") from error
        return value


class FetchConfig(StrictModel):
    """Politeness and network policy."""

    user_agent: str | None = Field(default=None, min_length=1, max_length=300)
    min_delay_seconds: float = Field(default=2.0, ge=0.5, le=3600)
    max_concurrent_hosts: int = Field(default=4, ge=1, le=32)
    allow_private_hosts: list[str] = Field(default_factory=list[str], max_length=50)


class ApiConfig(StrictModel):
    """Service bind address."""

    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)


class Config(StrictModel):
    """The whole sites.toml file."""

    version: Literal[1]
    defaults: Defaults = Field(default_factory=Defaults)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    fetch: FetchConfig = Field(default_factory=FetchConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    sites: list[SiteConfig] = Field(default_factory=list[SiteConfig], alias="site")


def derive_slug(url: str) -> str:
    """Build a site slug from host and path, dropping `www.`: portswigger-net-research."""
    parts = urlsplit(url)
    host = (parts.hostname or "").removeprefix("www.")
    slug = re.sub(r"[^a-z0-9]+", "-", f"{host}{parts.path}".lower()).strip("-")
    return slug[:48].rstrip("-") or "site"


def _find_line(lines: Sequence[str], loc: Sequence[str | int]) -> int | None:
    """Return the 1-based line of a key path like ('site', 1, 'url'), or None if not found."""
    header_re = re.compile(r"^\s*\[")
    keys = list(loc)
    start = -1
    if keys[:1] == ["site"] and len(keys) > 1 and isinstance(keys[1], int):
        headers = [i for i, line in enumerate(lines) if re.match(r"^\s*\[\[site\]\]", line)]
        if keys[1] >= len(headers):
            return None
        start, keys = headers[keys[1]], keys[2:]
    elif len(keys) > 1:
        table_re = re.compile(rf"^\s*\[{re.escape(str(keys[0]))}\]")
        found = [i for i, line in enumerate(lines) if table_re.match(line)]
        if not found:
            return None
        start, keys = found[0], keys[1:]
    fallback = start + 1 if start >= 0 else None
    if not keys:
        return fallback
    key_re = re.compile(rf"^\s*{re.escape(str(keys[0]))}\s*=")
    for i in range(start + 1, len(lines)):
        if header_re.match(lines[i]):
            break
        if key_re.match(lines[i]):
            return i + 1
    return fallback


def _location(loc: Sequence[str | int]) -> str:
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else part)
    return out


def _semantic_problems(config: Config) -> list[tuple[tuple[str | int, ...], str, str]]:
    found: list[tuple[tuple[str | int, ...], str, str]] = []
    allow = config.fetch.allow_private_hosts
    seen_slugs: dict[str, int] = {}
    seen_urls: dict[str, int] = {}
    for index, site in enumerate(config.sites):
        for key in ("url", "feed", "sitemap"):
            value = getattr(site, key)
            problem = value and url_policy_problem(value, allow)
            if problem:
                hint = "Use a public http(s) URL or add the host to [fetch] allow_private_hosts"
                found.append((("site", index, key), problem, hint))
        assert site.slug is not None
        if site.slug in seen_slugs:
            first = seen_slugs[site.slug]
            found.append((("site", index, "slug"), f"duplicate slug of site[{first}]", "Set slug"))
        seen_slugs.setdefault(site.slug, index)
        url_key = site.url.rstrip("/").lower()
        if url_key in seen_urls:
            first = seen_urls[url_key]
            found.append((("site", index, "url"), f"duplicate url of site[{first}]", "Remove one"))
        seen_urls.setdefault(url_key, index)
    if config.llm.provider != "anthropic":
        for key in ("notes_model", "lesson_model"):
            if getattr(config.llm, key) is None:
                message = f"{key} is required for provider {config.llm.provider}"
                found.append((("llm", key), message, f"Set {key} in [llm]"))
    if config.llm.provider == "openai_compatible" and config.llm.base_url is None:
        found.append((("llm", "base_url"), "base_url is required", "Set base_url in [llm]"))
    if config.notify.webhook_url and not config.notify.webhook_allow_private:
        problem = url_policy_problem(config.notify.webhook_url)
        if problem:
            hint = "Use a public URL or set webhook_allow_private = true"
            found.append((("notify", "webhook_url"), problem, hint))
    return found


def load_config(path: Path) -> tuple[Config | None, list[Problem]]:
    """Read and validate sites.toml.

    Returns (config, []) when valid, else (None, problems) listing every problem found.
    Schema errors are reported first; cross-field checks run only on a schema-valid file.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, [Problem(str(path), "config file not found", "Run: hackerblog init")]
    except OSError as error:
        return None, [Problem(str(path), f"cannot read config: {error.strerror}", "")]
    lines = text.splitlines()
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        line = getattr(error, "lineno", None)
        message = str(getattr(error, "msg", error))
        return None, [Problem(path.name, f"TOML syntax error: {message}", "Fix the syntax", line)]
    try:
        config = Config.model_validate(raw)
    except ValidationError as error:
        problems: list[Problem] = []
        for item in error.errors():
            loc = tuple(item["loc"])
            message = str(item["msg"]).removeprefix("Value error, ")
            hint = "Remove or rename the key" if item["type"] == "extra_forbidden" else ""
            problems.append(Problem(_location(loc), message, hint, _find_line(lines, loc)))
        return None, problems
    problems = [
        Problem(_location(loc), message, hint, _find_line(lines, loc))
        for loc, message, hint in _semantic_problems(config)
    ]
    return (None, problems) if problems else (config, [])
