"""Discovery use cases shared by every adapter: poll, sites, posts and the online check."""

import asyncio
import fcntl
import json
import os
import re
import sqlite3
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from hackerblog.core.config import Config, SiteConfig, derive_slug, load_config
from hackerblog.core.discovery.discover import Limits, SiteResult, discover_site
from hackerblog.core.discovery.opml import parse_opml
from hackerblog.core.discovery.resolve import Resolution, resolve
from hackerblog.core.errors import ExitCode, HackerBlogError
from hackerblog.core.net.fetcher import Fetcher, FetchError
from hackerblog.core.net.guard import BlockedError
from hackerblog.core.net.urls import url_policy_problem
from hackerblog.core.paths import Paths
from hackerblog.core.store import posts, sites
from hackerblog.core.store.migrate import connect, migrate
from hackerblog.core.store.sites import SiteRow, Trigger, utc_now

SITE_HEADER = re.compile(r"^\s*\[\[site\]\]\s*(#.*)?$")
TABLE_HEADER = re.compile(r"^\s*\[")
MIN_AGE = {"weekly": timedelta(days=6), "monthly": timedelta(days=27)}
OPML_BYTES_MAX = 5 * 1024 * 1024


def load_valid_config(paths: Paths) -> Config:
    """Load sites.toml or raise a config error that points at `hackerblog check`."""
    config, problems = load_config(paths.config)
    if config is None:
        assert problems
        first = problems[0].render(paths.config.name)
        count = len(problems)
        message = f"{count} problem{'s' if count > 1 else ''} in {paths.config.name}: {first}"
        raise HackerBlogError(ExitCode.CONFIG, message, "Run: hackerblog check")
    assert not problems
    return config


def open_store(paths: Paths, config: Config) -> sqlite3.Connection:
    """Open and migrate the database, then sync the configured sites into it."""
    if not paths.home.is_dir():
        raise HackerBlogError(
            ExitCode.CONFIG, f"{paths.home} does not exist", "Run: hackerblog init"
        )
    conn = connect(paths.db)
    migrate(conn)
    sites.sync_sites(conn, config.sites, utc_now())
    return conn


def scratch_store(paths: Paths, config: Config) -> sqlite3.Connection:
    """Return an in-memory copy of the database, so dry runs and checks write nothing."""
    scratch = sqlite3.connect(":memory:", isolation_level=None)
    scratch.execute("PRAGMA foreign_keys=ON")
    if paths.db.is_file():
        source = sqlite3.connect(f"file:{paths.db}?mode=ro", uri=True)
        try:
            source.backup(scratch)
        finally:
            source.close()
    migrate(scratch)
    sites.sync_sites(scratch, config.sites, utc_now())
    return scratch


@contextmanager
def poll_lock(paths: Paths, wait: bool) -> Generator[None]:
    """Hold the poll lock file; raise LOCKED at once when another poll holds it and not wait."""
    paths.locks.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(paths.locks / "poll.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            hint = "Wait for it to finish or add --wait"
            raise HackerBlogError(ExitCode.LOCKED, "another poll is running", hint) from error
        yield
    finally:
        os.close(fd)


@dataclass(frozen=True)
class PollReport:
    """Result of one discovery cycle over the selected sites."""

    poll_run_id: int | None
    results: list[SiteResult] = field(default_factory=list[SiteResult])
    dry_run: bool = False

    @property
    def code(self) -> ExitCode:
        """FAILED when every site failed, then BLOCKED, then PARTIAL, else OK (15.2)."""
        failed = [result for result in self.results if result.error]
        if not failed:
            return ExitCode.OK
        if len(failed) == len(self.results):
            return ExitCode.FAILED
        if any(result.blocked for result in failed):
            return ExitCode.BLOCKED
        return ExitCode.PARTIAL

    @property
    def new_posts(self) -> int:
        """Posts stored for the first time in this cycle."""
        return sum(result.new for result in self.results)


def _by_slug(config: Config) -> dict[str, SiteConfig]:
    found = {site.slug: site for site in config.sites if site.slug}
    assert len(found) == len(config.sites)
    return found


def _recently_polled(row: SiteRow, now: datetime, min_age: timedelta) -> bool:
    if row.last_success_at is None:
        return False
    last = datetime.strptime(row.last_success_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return now - last < min_age


def select_sites(
    conn: sqlite3.Connection, config: Config, slugs: Sequence[str]
) -> list[tuple[SiteRow, SiteConfig]]:
    """Pick the sites to poll: the named ones, or every enabled site that is due.

    Named sites must exist and be enabled. Without names, weekly sites polled successfully
    in the last 6 days and monthly sites in the last 27 days are left out (section 17,
    external mode).
    """
    configured = _by_slug(config)
    for slug in slugs:
        if slug not in configured:
            hint = "Try: hackerblog sites list"
            raise HackerBlogError(ExitCode.NOT_FOUND, f"no site named {slug!r}", hint)
    now = datetime.now(UTC)
    chosen: list[tuple[SiteRow, SiteConfig]] = []
    for row in sites.list_sites(conn, include_all=False):
        site = configured[row.slug]
        if slugs and row.slug not in slugs:
            continue
        schedule = site.schedule or config.defaults.schedule
        min_age = MIN_AGE.get(schedule)
        if not slugs and min_age is not None and _recently_polled(row, now, min_age):
            continue
        chosen.append((row, site))
    missing = set(slugs) - {row.slug for row, _ in chosen}
    if missing:
        hint = "Set enabled = true for it in sites.toml"
        raise HackerBlogError(ExitCode.USAGE, f"site {sorted(missing)[0]!r} is disabled", hint)
    return chosen


async def poll(
    paths: Paths,
    slugs: Sequence[str] = (),
    *,
    dry_run: bool = False,
    wait: bool = False,
    trigger: Trigger = "cli",
    sweep: bool = False,
) -> PollReport:
    """Discover new posts on the selected sites and queue them for processing.

    sweep runs every site's archive pass (section 8.5), as `backfill` does. A dry run works
    on an in-memory copy of the database with HTTP caches untouched, so it writes nothing
    and takes no lock.
    """
    config = load_valid_config(paths)
    if dry_run:
        store = scratch_store(paths, config)
        return await _poll(store, config, slugs, trigger, dry_run=True, sweep=sweep)
    with poll_lock(paths, wait):
        store = open_store(paths, config)
        return await _poll(store, config, slugs, trigger, dry_run=False, sweep=sweep)


async def _poll(
    conn: sqlite3.Connection,
    config: Config,
    slugs: Sequence[str],
    trigger: Trigger,
    *,
    dry_run: bool,
    sweep: bool,
) -> PollReport:
    try:
        targets = select_sites(conn, config, slugs)
        run_id = sites.start_poll_run(conn, trigger, utc_now())
        fetcher = Fetcher(conn, config.fetch, persist=not dry_run)
        async with fetcher, asyncio.TaskGroup() as group:
            tasks = [
                group.create_task(
                    discover_site(
                        conn,
                        fetcher,
                        row,
                        site,
                        limits=Limits.of(site, config.defaults),
                        now=utc_now(),
                        sweep=sweep,
                    )
                )
                for row, site in targets
            ]
        report = PollReport(None if dry_run else run_id, [task.result() for task in tasks], dry_run)
        ok = sum(1 for result in report.results if result.error is None)
        sites.finish_poll_run(
            conn,
            run_id,
            utc_now(),
            sites_ok=ok,
            sites_failed=len(report.results) - ok,
            new_posts=report.new_posts,
        )
    finally:
        conn.close()
    assert len(report.results) == len(targets)
    return report


@dataclass(frozen=True)
class OnlineResult:
    """One site's resolution during `check --online`."""

    slug: str
    resolution: Resolution | None
    error: str | None

    def __post_init__(self) -> None:
        assert (self.resolution is None) != (self.error is None)


async def check_online(paths: Paths) -> list[OnlineResult]:
    """Fetch robots and resolve the source of every enabled site without writing anything."""
    config = load_valid_config(paths)
    conn = scratch_store(paths, config)
    try:
        enabled = [site for site in config.sites if site.enabled is not False]
        fetcher = Fetcher(conn, config.fetch, persist=False)
        async with fetcher, asyncio.TaskGroup() as group:
            tasks = [group.create_task(_resolve_one(fetcher, site)) for site in enabled]
    finally:
        conn.close()
    found = [task.result() for task in tasks]
    assert len(found) == len(enabled)
    return found


async def _resolve_one(fetcher: Fetcher, site: SiteConfig) -> OnlineResult:
    assert site.slug is not None
    try:
        return OnlineResult(site.slug, await resolve(fetcher, site), None)
    except (BlockedError, FetchError) as error:
        return OnlineResult(site.slug, None, error.message)


@dataclass(frozen=True)
class NewSite:
    """Options of `sites add`; None means inherit or derive."""

    url: str
    name: str | None = None
    slug: str | None = None
    tags: tuple[str, ...] = ()
    feed: str | None = None
    notify: str | None = None
    backfill_limit: int | None = None
    schedule: str | None = None
    limit: int | None = None
    aggregator: bool = False


@dataclass(frozen=True)
class Added:
    """The site appended to sites.toml and what discovery found for it."""

    site: SiteConfig
    resolution: Resolution | None


def toml_value(value: object) -> str:
    """Render a str, int, bool or list of str as a TOML value.

    JSON escaping without ensure_ascii fits TOML basic strings, except DEL, which TOML
    requires escaped and JSON leaves raw; astral characters stay literal, since TOML
    rejects the surrogate pairs ensure_ascii would write.
    """
    assert isinstance(value, str | int | bool | list)
    if isinstance(value, bool):
        return "true" if value else "false"
    rendered = json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    assert "\x7f" not in rendered
    return rendered


def toml_block(values: dict[str, object]) -> str:
    """Render one [[site]] table from values, leaving out None and empty lists."""
    lines = ["[[site]]"]
    for key, value in values.items():
        if value is None or value in ((), []):
            continue
        lines.append(f"{key} = {toml_value(value)}")
    assert len(lines) >= 2
    return "\n".join(lines) + "\n"


def append_sites(paths: Paths, blocks: Sequence[dict[str, object]]) -> Config:
    """Append [[site]] tables to sites.toml as text and re-validate the whole file.

    The previous bytes are restored when the result does not validate, so a bad append
    never leaves a broken config behind. Nothing else in the file is rewritten.
    """
    assert blocks
    before = paths.config.read_bytes()
    separator = (
        b""
        if before.endswith(b"\n\n") or not before
        else b"\n"
        if before.endswith(b"\n")
        else b"\n\n"
    )
    text = "\n".join(toml_block(block) for block in blocks)
    paths.config.write_bytes(before + separator + text.encode("utf-8"))
    config, problems = load_config(paths.config)
    if config is None:
        paths.config.write_bytes(before)
        first = problems[0].render(paths.config.name)
        raise HackerBlogError(ExitCode.CONFIG, f"the new site is invalid: {first}", "")
    return config


def _site_key(url: str) -> str:
    return url.rstrip("/").lower()


def _present(values: dict[str, object]) -> dict[str, object]:
    kept = {key: value for key, value in values.items() if value is not None and value != []}
    assert len(kept) <= len(values)
    return kept


def _validated(config: Config, values: dict[str, object]) -> SiteConfig:
    """Validate one new site against the schema, the URL policy and existing sites."""
    try:
        site = SiteConfig.model_validate(values)
    except ValueError as error:
        raise HackerBlogError(ExitCode.CONFIG, f"invalid site: {error}", "") from error
    allow = config.fetch.allow_private_hosts
    for key in ("url", "feed"):
        value = values.get(key)
        problem = isinstance(value, str) and url_policy_problem(value, allow)
        if problem:
            raise HackerBlogError(ExitCode.BLOCKED, f"{value} refused: {problem}", "")
    if any(_site_key(existing.url) == _site_key(site.url) for existing in config.sites):
        raise HackerBlogError(ExitCode.CONFIG, f"{site.url} is already followed", "")
    if any(existing.slug == site.slug for existing in config.sites):
        hint = "Pass --slug with another name"
        raise HackerBlogError(ExitCode.CONFIG, f"slug {site.slug} already exists", hint)
    return site


async def add_site(paths: Paths, spec: NewSite, discover: bool) -> Added:
    """Validate a new site, resolve its source unless discover is false, and append it.

    The page title found by discovery becomes the name when none is given.
    """
    config = load_valid_config(paths)
    values: dict[str, object] = {
        "name": spec.name,
        "url": spec.url,
        "slug": spec.slug,
        "feed": spec.feed,
        "tags": list(spec.tags),
        "notify": spec.notify,
        "backfill_limit": spec.backfill_limit,
        "schedule": spec.schedule,
        "limit": spec.limit,
        "aggregator": spec.aggregator or None,
    }
    site = _validated(config, _present(values))
    resolution: Resolution | None = None
    if discover:
        conn = scratch_store(paths, config)
        try:
            async with Fetcher(conn, config.fetch, persist=False) as fetcher:
                resolution = await resolve(fetcher, site)
        finally:
            conn.close()
        values["name"] = spec.name or resolution.title
    appended = append_sites(paths, [values])
    stored = next(item for item in appended.sites if _site_key(item.url) == _site_key(site.url))
    assert stored.slug == site.slug
    return Added(stored, resolution)


def _edited_lines(lines: list[str], index: int, changes: dict[str, object | None]) -> list[str]:
    """Set or remove keys in the index-th [[site]] table of lines, leaving other lines as is."""
    headers = [i for i, line in enumerate(lines) if SITE_HEADER.match(line)]
    if index >= len(headers):
        hint = "Write each site as a [[site]] table"
        raise HackerBlogError(ExitCode.CONFIG, "cannot find the site's [[site]] table", hint)
    start = headers[index]
    end = next(
        (i for i in range(start + 1, len(lines)) if TABLE_HEADER.match(lines[i])), len(lines)
    )
    block = lines[start:end]
    for key, value in changes.items():
        key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
        at = next((i for i, line in enumerate(block) if key_re.match(line)), None)
        line = None if value is None else f"{key} = {toml_value(value)}\n"
        if at is not None:
            block[at : at + 1] = [] if line is None else [line]
        elif line is not None:
            body = [
                i
                for i, text in enumerate(block)
                if text.strip() and not text.lstrip().startswith("#")
            ]
            block.insert(body[-1] + 1, line)
    assert block and SITE_HEADER.match(block[0])
    return [*lines[:start], *block, *lines[end:]]


def edit_site(paths: Paths, slug: str, changes: dict[str, object | None]) -> SiteConfig:
    """Set (or, for None, remove) keys of one [[site]] table in sites.toml in place.

    Only the changed key lines are rewritten. The previous bytes are restored when the
    file no longer validates or another site changed, so a bad edit never sticks.
    """
    assert changes
    config = load_valid_config(paths)
    index = next((i for i, site in enumerate(config.sites) if site.slug == slug), None)
    if index is None:
        hint = "Try: hackerblog sites list --all"
        raise HackerBlogError(ExitCode.NOT_FOUND, f"no site named {slug!r}", hint)
    before = paths.config.read_bytes()
    lines = before.decode("utf-8").splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    paths.config.write_bytes("".join(_edited_lines(lines, index, changes)).encode("utf-8"))
    edited, problems = load_config(paths.config)
    if edited is None:
        paths.config.write_bytes(before)
        first = problems[0].render(paths.config.name)
        raise HackerBlogError(ExitCode.CONFIG, f"the edit is invalid: {first}", "")
    others = [site for i, site in enumerate(config.sites) if i != index]
    if [site for i, site in enumerate(edited.sites) if i != index] != others:
        paths.config.write_bytes(before)
        raise HackerBlogError(ExitCode.CONFIG, "the edit would change another site", "")
    site = edited.sites[index]
    assert site.slug == slug
    return site


@dataclass(frozen=True)
class OpmlImport:
    """What `sites import-opml` added, skipped as already followed, and found invalid."""

    added: list[SiteConfig]
    existing: list[str]
    invalid: list[str]


def _free_slug(slug: str, taken: set[str | None]) -> str:
    """Return slug, or slug-2, slug-3 and so on, whichever is not taken yet."""
    candidate = slug
    for number in range(2, len(taken) + 3):
        if candidate not in taken:
            return candidate
        candidate = f"{slug[:44]}-{number}"
    raise AssertionError("unreachable: more numbers than taken slugs")


def import_opml(paths: Paths, opml: Path, tags: Sequence[str], dry_run: bool) -> OpmlImport:
    """Append one [[site]] per OPML outline that is valid and not followed yet."""
    config = load_valid_config(paths)
    with opml.open("rb") as handle:
        body = handle.read(OPML_BYTES_MAX + 1)
    if len(body) > OPML_BYTES_MAX:
        raise HackerBlogError(ExitCode.USAGE, f"{opml} is larger than 5 MiB", "")
    try:
        entries, invalid = parse_opml(body)
    except ValueError as error:
        raise HackerBlogError(ExitCode.USAGE, f"{opml} is not valid OPML: {error}", "") from error
    added: list[SiteConfig] = []
    blocks: list[dict[str, object]] = []
    existing: list[str] = []
    followed = {_site_key(site.url) for site in config.sites}
    slugs = {site.slug for site in config.sites}
    for entry in entries:
        if _site_key(entry.url) in followed:
            existing.append(entry.url)
            continue
        slug = _free_slug(derive_slug(entry.url), slugs)
        values: dict[str, object] = {
            "name": (entry.title or "").strip()[:80] or None,
            "url": entry.url,
            "slug": slug,
            "feed": entry.feed,
            "tags": list(tags),
        }
        current = config.model_copy(update={"sites": [*config.sites, *added]})
        try:
            site = _validated(current, _present(values))
        except HackerBlogError as error:
            invalid.append(f"{entry.url}: {error.message}")
            continue
        followed.add(_site_key(entry.url))
        slugs.add(slug)
        added.append(site)
        blocks.append(values)
    if blocks and not dry_run:
        append_sites(paths, blocks)
    assert len(added) == len(blocks)
    return OpmlImport(added, existing, invalid)


@dataclass(frozen=True)
class SiteSummary:
    """One site as `sites list` and `sites show` print it."""

    row: SiteRow
    config: SiteConfig | None
    schedule: str | None
    notify: str | None
    counts: dict[str, int]
    health: str


@dataclass(frozen=True)
class SiteDetail:
    """`sites show`: the summary plus limits, HTTP validators and the robots policy."""

    summary: SiteSummary
    limits: Limits | None
    validators: list[tuple[str, str | None, str | None, int | None, str]]
    robots: tuple[str, str] | None


def _health(row: SiteRow, unhealthy_after: int) -> str:
    assert unhealthy_after >= 1
    if row.consecutive_failures == 0:
        return "ok"
    if row.consecutive_failures >= unhealthy_after:
        return "unhealthy"
    return f"failing {row.consecutive_failures}"


def _summary(conn: sqlite3.Connection, config: Config, row: SiteRow) -> SiteSummary:
    site = _by_slug(config).get(row.slug)
    defaults = config.defaults
    return SiteSummary(
        row=row,
        config=site,
        schedule=None if site is None else site.schedule or defaults.schedule,
        notify=None if site is None else site.notify or defaults.notify,
        counts=posts.status_counts(conn, row.id),
        health=_health(row, config.notify.unhealthy_after_failures),
    )


def site_summaries(paths: Paths, include_all: bool) -> list[SiteSummary]:
    """Return every active site, or every site in any status when include_all."""
    config = load_valid_config(paths)
    conn = open_store(paths, config)
    try:
        return [_summary(conn, config, row) for row in sites.list_sites(conn, include_all)]
    finally:
        conn.close()


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def site_detail(paths: Paths, slug: str) -> SiteDetail:
    """Return one site's detail, or raise NOT_FOUND."""
    config = load_valid_config(paths)
    conn = open_store(paths, config)
    try:
        row = sites.get_site(conn, slug)
        if row is None:
            hint = "Try: hackerblog sites list --all"
            raise HackerBlogError(ExitCode.NOT_FOUND, f"no site named {slug!r}", hint)
        summary = _summary(conn, config, row)
        urls = [url for url in (row.url, row.feed_url, row.sitemap_url) if url]
        validators = conn.execute(
            "SELECT url, etag, last_modified, last_status, fetched_at FROM http_cache "
            "WHERE url IN (SELECT value FROM json_each(?)) ORDER BY url",
            (json.dumps(urls),),
        ).fetchall()
        robots = conn.execute(
            "SELECT policy, fetched_at FROM robots_cache WHERE origin = ?", (_origin(row.url),)
        ).fetchone()
    finally:
        conn.close()
    site = summary.config
    limits = None if site is None else Limits.of(site, config.defaults)
    return SiteDetail(summary, limits, [tuple(item) for item in validators], robots)


def list_posts(paths: Paths, query: posts.PostQuery) -> list[posts.PostRow]:
    """Return posts matching query; unknown site slugs raise NOT_FOUND."""
    config = load_valid_config(paths)
    conn = open_store(paths, config)
    try:
        for slug in query.sites:
            if sites.get_site(conn, slug) is None:
                hint = "Try: hackerblog sites list --all"
                raise HackerBlogError(ExitCode.NOT_FOUND, f"no site named {slug!r}", hint)
        return posts.list_posts(conn, query)
    finally:
        conn.close()


def get_post(paths: Paths, post_id: int) -> posts.PostRow:
    """Return one post or raise NOT_FOUND."""
    config = load_valid_config(paths)
    conn = open_store(paths, config)
    try:
        found = posts.get_post(conn, post_id)
    finally:
        conn.close()
    if found is None:
        hint = "Try: hackerblog posts list --status any"
        raise HackerBlogError(ExitCode.NOT_FOUND, f"no post with id {post_id}", hint)
    assert found.id == post_id
    return found
