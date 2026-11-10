"""Processing with the real page fetcher: the queue runner and the process and backfill commands.

poll, work, process, backfill and serve all fetch pages through net.fetcher.Fetcher, built
from the [fetch] table of sites.toml, so processing gets the same SSRF guard, robots rules and
size caps as discovery (docs/architecture.md sections 9 and 15.3).
"""

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from hackerblog.core import jobs, pipeline
from hackerblog.core.discovery import service as discovery
from hackerblog.core.errors import ExitCode, HackerBlogError
from hackerblog.core.net.fetcher import Fetcher
from hackerblog.core.net.urls import canonicalize, url_policy_problem
from hackerblog.core.paths import Paths
from hackerblog.core.store import posts, repo
from hackerblog.core.store.sites import utc_now

TOKENS_PER_POST_ESTIMATE = 40_000
SERVE_CONCURRENCY = 2
SERVE_PAUSE_S = 60.0
DUE_SQL = (
    "SELECT 1 FROM jobs WHERE (state = 'pending' AND run_after <= :now)"
    " OR (state = 'running' AND lease_until < :now) LIMIT 1"
)
logger = logging.getLogger("hackerblog.work")


async def run_queue(
    paths: Paths,
    backend: pipeline.Backend,
    *,
    max_jobs: int | None = None,
    concurrency: int = 1,
    forever: bool = False,
    on_result: Callable[[pipeline.JobResult], None] | None = None,
) -> pipeline.WorkReport:
    """Run due process_post jobs with a Fetcher until the queue is empty (see run_worker).

    Returns an empty report without reading LLM settings when no job is due and not forever.
    Raises HackerBlogError when sites.toml, the database or the LLM settings are not usable.
    """
    config = discovery.load_valid_config(paths)
    conn = discovery.open_store(paths, config)
    try:
        if not forever and conn.execute(DUE_SQL, {"now": utc_now()}).fetchone() is None:
            return pipeline.WorkReport([])
        settings = pipeline.load_settings(paths, need_llm=True)
        async with Fetcher(conn, config.fetch) as fetcher:
            return await pipeline.run_worker(
                conn,
                settings,
                backend,
                fetcher=fetcher,
                max_jobs=max_jobs,
                concurrency=concurrency,
                forever=forever,
                on_result=on_result,
            )
    finally:
        conn.close()


async def keep_working(paths: Paths, backend: pipeline.Backend) -> None:
    """Process the queue forever for serve; a stop such as the budget pauses, never ends it."""
    while True:
        try:
            report = await run_queue(paths, backend, concurrency=SERVE_CONCURRENCY, forever=True)
            stop: HackerBlogError | None = report.stop
        except HackerBlogError as error:
            stop = error
        assert stop is not None, "a forever worker returns only on a stop"
        logger.warning("worker.paused", extra={"error": stop.message})
        await asyncio.sleep(SERVE_PAUSE_S)


@dataclass(frozen=True)
class Processed:
    """A URL handled by `process`: its post, site slug, and outcome (None when only queued)."""

    post_id: int
    site: str
    outcome: pipeline.Outcome | None


def _site_for(conn: sqlite3.Connection, url: str, slug: str | None) -> tuple[int, str]:
    if slug is not None:
        row = conn.execute("SELECT id FROM sites WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            hint = "Try: hackerblog sites list"
            raise HackerBlogError(ExitCode.NOT_FOUND, f"no site named {slug!r}", hint)
        return int(row[0]), slug
    host = urlsplit(url).hostname
    for site_id, site_slug, site_url in conn.execute(
        "SELECT id, slug, url FROM sites WHERE slug != ? ORDER BY id", (repo.ADHOC_SLUG,)
    ):
        if host and urlsplit(str(site_url)).hostname == host:
            return int(site_id), str(site_slug)
    return repo.ensure_adhoc_site(conn), repo.ADHOC_SLUG


async def process_url(
    paths: Paths,
    backend: pipeline.Backend,
    url: str,
    *,
    site: str | None = None,
    force: bool = False,
    wait: bool = True,
) -> Processed:
    """Add url as a manual post and process it now, or only queue it when not wait.

    A post already ready is returned at zero tokens unless force, which bypasses the LLM
    cache. Raises HackerBlogError: BLOCKED for a refused URL, NOT_FOUND for an unknown site,
    and the processing error otherwise.
    """
    config = discovery.load_valid_config(paths)
    problem = url_policy_problem(url, config.fetch.allow_private_hosts)
    if problem is not None:
        raise HackerBlogError(ExitCode.BLOCKED, f"{url} is refused: {problem}")
    conn = discovery.open_store(paths, config)
    try:
        site_id, slug = _site_for(conn, url, site)
        post_id, _ = repo.insert_manual_post(conn, site_id, url, canonicalize(url))
        post = repo.get_post(conn, post_id)
        assert post is not None
        if not wait:
            jobs.enqueue(conn, post_id, "all", datetime.now(UTC))
            return Processed(post_id, slug, None)
        if post.status == "ready" and not force:
            return Processed(post_id, slug, pipeline.Outcome(post_id, "ready", 0))
        settings = pipeline.load_settings(paths, need_llm=True)
        try:
            async with Fetcher(conn, config.fetch) as fetcher:
                outcome = await pipeline.process_post(
                    conn, post_id, settings, backend, fetcher=fetcher, force=force
                )
        except Exception as failure:
            error = pipeline.as_error(failure)
            if error is None:
                raise
            repo.set_post_status(conn, post_id, "failed", error=error.message)
            raise error from failure
        conn.execute(
            "UPDATE jobs SET state = 'cancelled', updated_at = ?"
            " WHERE post_id = ? AND state = 'pending'",
            (utc_now(), post_id),
        )
        return Processed(post_id, slug, outcome)
    finally:
        conn.close()


@dataclass(frozen=True)
class Backfill:
    """What backfill found and queued: the discovery report, post ids, and a token estimate."""

    discovery: discovery.PollReport
    queued: list[int]
    estimate_tokens: int

    def __post_init__(self) -> None:
        assert self.estimate_tokens == len(self.queued) * TOKENS_PER_POST_ESTIMATE


async def backfill(
    paths: Paths,
    slug: str,
    *,
    limit: int | None,
    since: str | None = None,
    dry_run: bool = False,
    wait: bool = False,
) -> Backfill:
    """Discover the site, then re-queue its backfill_limit skips, newest first, up to limit.

    limit None means the site's backfill_limit, else no cap; already processed posts count
    toward it. A dry run discovers without writing and queues nothing.
    """
    report = await discovery.poll(paths, [slug], dry_run=dry_run, wait=wait, sweep=True)
    config = discovery.load_valid_config(paths)
    site = next(site for site in config.sites if site.slug == slug)
    cap = limit
    if cap is None:
        cap = site.backfill_limit
    if cap is None:
        cap = config.defaults.backfill_limit
    conn = discovery.open_store(paths, config)
    try:
        done = conn.execute(
            "SELECT COUNT(*) FROM posts p JOIN sites s ON s.id = p.site_id"
            " WHERE s.slug = ? AND p.status NOT IN ('discovered', 'skipped')",
            (slug,),
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT p.id FROM posts p JOIN sites s ON s.id = p.site_id"
            " WHERE s.slug = ? AND p.status = 'skipped' AND p.skip_reason = 'backfill_limit'"
            " AND (? IS NULL OR p.published_at >= ?)"
            " ORDER BY p.published_at DESC, p.id DESC",
            (slug, since, since),
        ).fetchall()
        room = len(rows) if cap is None else max(0, cap - int(done))
        chosen = [int(row[0]) for row in rows[:room]]
        if not dry_run:
            now = utc_now()
            for post_id in chosen:
                posts.queue_post(conn, post_id, now)
    finally:
        conn.close()
    return Backfill(report, chosen, len(chosen) * TOKENS_PER_POST_ESTIMATE)
