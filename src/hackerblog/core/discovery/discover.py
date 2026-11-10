"""Discover one site: resolve its source, collect candidates and store them (8.3 to 8.5)."""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from hackerblog.core.config import Defaults, SiteConfig
from hackerblog.core.discovery.feeds import parse_feed
from hackerblog.core.discovery.linkdiff import extract_links, is_post_url, post_roots
from hackerblog.core.discovery.model import URL_LENGTH_MAX, Candidate
from hackerblog.core.discovery.resolve import resolve
from hackerblog.core.discovery.sitemaps import parse_sitemap
from hackerblog.core.net.fetcher import Fetcher, FetchError, Response
from hackerblog.core.net.guard import BlockedError
from hackerblog.core.net.robots import origin_of
from hackerblog.core.net.urls import canonicalize
from hackerblog.core.store import posts, sites
from hackerblog.core.store.sites import SiteRow, transaction

SITEMAP_DEPTH_MAX = 2
SITEMAP_CHILDREN_MAX = 50
RESOLVE_AFTER_FAILURES = 3


@dataclass
class Collected:
    """Candidates and responses from every source fetched, and how many answered 304."""

    candidates: list[Candidate] = field(default_factory=list[Candidate])
    fetched: int = 0
    not_modified: int = 0
    responses: list[Response] = field(default_factory=list[Response])

    @property
    def all_not_modified(self) -> bool:
        """True when at least one source was fetched and every one answered 304."""
        assert self.not_modified <= self.fetched
        return self.fetched > 0 and self.not_modified == self.fetched


@dataclass(frozen=True)
class SiteResult:
    """What one site's discovery found and stored."""

    slug: str
    method: str | None = None
    first_sync: bool = False
    found: int = 0
    new: int = 0
    queued: int = 0
    skipped: int = 0
    not_modified: bool = False
    error: str | None = None
    blocked: bool = False

    def __post_init__(self) -> None:
        assert self.new <= self.found
        assert not self.blocked or self.error is not None


@dataclass(frozen=True)
class Limits:
    """The per-site caps after [defaults] are applied; limit is per site only, None is unset."""

    backfill_limit: int | None
    max_new_per_poll: int
    limit: int | None = None

    @classmethod
    def of(cls, site: SiteConfig, defaults: Defaults) -> "Limits":
        """Use the site's value when set, else the default; a None backfill limit is no cap."""
        backfill = (
            site.backfill_limit if site.backfill_limit is not None else defaults.backfill_limit
        )
        per_poll = site.max_new_per_poll or defaults.max_new_per_poll
        assert backfill is None or backfill >= 0
        assert per_poll >= 1
        assert site.limit is None or site.limit >= 1
        return cls(backfill, per_poll, site.limit)


async def discover_site(
    conn: sqlite3.Connection,
    fetcher: Fetcher,
    row: SiteRow,
    config: SiteConfig,
    *,
    limits: Limits,
    now: str,
    sweep: bool = False,
) -> SiteResult:
    """Run discovery for one site and record the poll result on the site.

    sweep forces the archive pass with unconditional requests, as `backfill` needs. Network
    and policy failures become a SiteResult with an error; anything else raises.
    """
    assert row.slug == config.slug
    assert row.status == "active"
    try:
        result = await _discover(conn, fetcher, row, config, limits=limits, now=now, sweep=sweep)
    except BlockedError as error:
        result = SiteResult(row.slug, row.discovery_method, error=error.message, blocked=True)
    except FetchError as error:
        result = SiteResult(row.slug, row.discovery_method, error=error.message)
    sites.record_poll(conn, row.id, now, result.error)
    return result


async def _discover(
    conn: sqlite3.Connection,
    fetcher: Fetcher,
    row: SiteRow,
    config: SiteConfig,
    *,
    limits: Limits,
    now: str,
    sweep: bool,
) -> SiteResult:
    if row.discovery_method is None or row.consecutive_failures >= RESOLVE_AFTER_FAILURES:
        found = await resolve(fetcher, config)
        sites.record_resolution(
            conn,
            row.id,
            found.method,
            feed_url=found.feed_url,
            sitemap_url=found.sitemap_url,
            title=found.title,
        )
        refreshed = sites.get_site(conn, row.slug)
        assert refreshed is not None
        row = refreshed
    assert row.discovery_method is not None
    first_sync = row.backfilled_at is None
    collected = await _collect(
        conn,
        fetcher,
        row,
        archive=first_sync or sweep,
        sweep=sweep,
        allowed=config.allowed_hosts,
        aggregator=config.aggregator,
    )
    blocked = set(await screen(fetcher, row, collected.candidates))
    with transaction(conn):
        if first_sync:
            stored = store_backfill(
                conn, row.id, collected.candidates, blocked, limits=limits, now=now
            )
            sites.mark_backfilled(conn, row.id, now)
        else:
            stored = store_new(conn, row.id, collected.candidates, blocked, limits=limits, now=now)
        for response in collected.responses:
            fetcher.remember(response)
    return SiteResult(
        slug=row.slug,
        method=row.discovery_method,
        first_sync=first_sync,
        found=len(collected.candidates),
        new=stored[0],
        queued=stored[1],
        skipped=stored[2],
        not_modified=collected.all_not_modified,
    )


async def screen(fetcher: Fetcher, row: SiteRow, candidates: list[Candidate]) -> dict[str, str]:
    """Return the skip reason for candidates robots.txt disallows, keyed by URL.

    Only the site's own origins are checked, so a hostile feed linking elsewhere cannot
    fail the site or fan out robots fetches; the fetch-time guard covers other URLs.
    """
    origins = {origin_of(url) for url in (row.url, row.feed_url) if url}
    assert 1 <= len(origins) <= 2
    reasons: dict[str, str] = {}
    for item in candidates:
        if origin_of(item.url) in origins and not await fetcher.allowed(item.url):
            reasons[item.url] = "robots"
    assert len(reasons) <= len(candidates)
    return reasons


async def _collect(
    conn: sqlite3.Connection,
    fetcher: Fetcher,
    row: SiteRow,
    *,
    archive: bool,
    sweep: bool,
    allowed: Sequence[str],
    aggregator: bool,
) -> Collected:
    """Fetch the site's sources and gather candidates, deduped by canonical URL.

    A routine poll reads the site's one method. The archive pass (first sync, sweep, or a
    feed holding no known post, so older ones may have fallen out of it) reads the feed,
    the sitemap and the index page together, because feeds carry only the latest entries.
    Aggregator sites never run it: their feed is the whole source and rarely repeats.
    """
    collected = Collected()
    conditional = not sweep
    if row.discovery_method == "feed":
        assert row.feed_url is not None
        await _collect_feed(
            fetcher,
            row,
            row.feed_url,
            collected,
            allowed,
            conditional=conditional,
            aggregator=aggregator,
        )
        archive = archive or _feed_gap(conn, collected.candidates)
    archive = archive and not aggregator
    roots = post_roots(row.url, [item.url for item in collected.candidates])
    if row.sitemap_url and (archive or row.discovery_method == "sitemap"):
        await _collect_sitemap(
            fetcher, row, row.sitemap_url, collected, roots, conditional=conditional
        )
    if archive or row.discovery_method == "linkdiff":
        await _collect_links(fetcher, row, collected, roots, conditional)
    unique = {item.url: item for item in reversed(collected.candidates)}
    collected.candidates = list(reversed(unique.values()))
    assert len(collected.candidates) == len(unique)
    return collected


def _feed_gap(conn: sqlite3.Connection, entries: list[Candidate]) -> bool:
    """True when the feed lists posts but none is stored yet, so a gap may sit behind it."""
    return bool(entries) and not posts.known_urls(conn, [item.url for item in entries])


def _checked(url: str, status: int) -> None:
    if status not in {200, 304}:
        raise FetchError(f"{url} answered HTTP {status}", status=status)


def _bare_host(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")


def own_entries(
    site_url: str,
    entries: list[Candidate],
    allowed_hosts: Sequence[str] = (),
    *,
    aggregator: bool = False,
) -> list[Candidate]:
    """Keep feed entries on the site's host, its subdomains, or an allowed_hosts entry.

    canonical_url is unique across sites, so an entry on an unrelated host could claim a
    post of another followed site or queue arbitrary third-party pages. Sibling hosts
    need allowed_hosts, since telling them apart needs the public suffix list. An
    aggregator site (section 19, T15) keeps every entry; fetches stay guarded.
    """
    host = _bare_host(site_url)
    assert host
    if aggregator:
        return list(entries)
    allowed = {name.lower() for name in allowed_hosts}

    def own(url: str) -> bool:
        entry = _bare_host(url)
        return entry == host or entry.endswith("." + host) or entry in allowed

    kept = [item for item in entries if own(item.url)]
    assert len(kept) <= len(entries)
    return kept


async def _collect_feed(
    fetcher: Fetcher,
    row: SiteRow,
    url: str,
    collected: Collected,
    allowed_hosts: Sequence[str],
    *,
    conditional: bool,
    aggregator: bool,
) -> None:
    response = await fetcher.get(url, kind="feed", conditional=conditional)
    _checked(url, response.status)
    collected.responses.append(response)
    collected.fetched += 1
    if response.not_modified:
        collected.not_modified += 1
        return
    entries = parse_feed(response.body, response.url)
    if entries is None:
        raise FetchError(f"{url} is no longer a feed with entries", status=response.status)
    collected.candidates.extend(own_entries(row.url, entries, allowed_hosts, aggregator=aggregator))


async def _collect_sitemap(
    fetcher: Fetcher,
    row: SiteRow,
    root: str,
    collected: Collected,
    roots: Sequence[str],
    *,
    conditional: bool,
) -> None:
    """Walk a sitemap and its indexes breadth first, depth and child count bounded."""
    pending: list[tuple[str, int]] = [(root, 0)]
    visited: set[str] = set()
    while pending and len(visited) <= SITEMAP_CHILDREN_MAX:
        url, depth = pending.pop(0)
        if url in visited:
            continue
        visited.add(url)
        response = await fetcher.get(url, kind="sitemap", conditional=conditional)
        _checked(url, response.status)
        collected.responses.append(response)
        collected.fetched += 1
        if response.not_modified:
            collected.not_modified += 1
            continue
        try:
            parsed = parse_sitemap(response.body)
        except ValueError as error:
            raise FetchError(f"{url} is not a valid sitemap: {error}") from error
        collected.candidates.extend(post_candidates(row, parsed.pages, roots))
        if depth < SITEMAP_DEPTH_MAX:
            pending.extend((child, depth + 1) for child in parsed.children)
    assert len(visited) <= SITEMAP_CHILDREN_MAX + 1


def post_candidates(
    row: SiteRow, pages: list[tuple[str, str | None]], roots: Sequence[str] = ()
) -> list[Candidate]:
    """Keep canonical same-host URLs that look like posts; unusable URLs are dropped.

    roots are extra post directories from post_roots, passed on to is_post_url.
    """
    host = urlsplit(row.url).hostname
    found: list[Candidate] = []
    for loc, lastmod in pages:
        if len(loc) > URL_LENGTH_MAX:
            continue
        try:
            url = canonicalize(loc)
        except ValueError:
            continue
        if urlsplit(url).hostname == host and is_post_url(
            url, row.url, row.post_url_pattern, roots
        ):
            found.append(Candidate(url=url, guid=url, published_at=lastmod, source_url=loc))
    assert len(found) <= len(pages)
    return found


async def _collect_links(
    fetcher: Fetcher, row: SiteRow, collected: Collected, roots: Sequence[str], conditional: bool
) -> None:
    response = await fetcher.get(row.url, kind="page", conditional=conditional)
    _checked(row.url, response.status)
    collected.responses.append(response)
    collected.fetched += 1
    if response.not_modified:
        collected.not_modified += 1
        return
    links = extract_links(response.body, response.url)
    pages: list[tuple[str, str | None]] = [(link, None) for link in links]
    collected.candidates.extend(post_candidates(row, pages, roots))


def _newest_first(candidates: list[Candidate]) -> list[Candidate]:
    dated = sorted(
        (item for item in candidates if item.published_at),
        key=lambda item: item.published_at or "",
        reverse=True,
    )
    undated = [item for item in candidates if not item.published_at]
    assert len(dated) + len(undated) == len(candidates)
    return dated + undated


def store_backfill(
    conn: sqlite3.Connection,
    site_id: int,
    candidates: list[Candidate],
    blocked: set[str],
    *,
    limits: Limits,
    now: str,
) -> tuple[int, int, int]:
    """Insert every candidate as backfill; queue the newest backfill_limit, skip the rest.

    With a site limit, candidates keep source order and only the first limit unblocked
    ones may be queued; the others are skipped as over_limit, which nothing re-queues.
    """
    new = queued = skipped = taken = 0
    ordered = candidates if limits.limit is not None else _newest_first(candidates)
    for item in ordered:
        post_id = posts.insert_post(conn, site_id, item, "backfill", now)
        if post_id is None:
            continue
        new += 1
        if item.url in blocked:
            posts.skip_post(conn, post_id, "robots", now)
            skipped += 1
            continue
        taken += 1
        if limits.limit is not None and taken > limits.limit:
            posts.skip_post(conn, post_id, "over_limit", now)
            skipped += 1
        elif limits.backfill_limit is None or queued < limits.backfill_limit:
            posts.queue_post(conn, post_id, now)
            queued += 1
        else:
            posts.skip_post(conn, post_id, "backfill_limit", now)
            skipped += 1
    assert queued + skipped == new
    assert limits.limit is None or queued <= limits.limit
    return new, queued, skipped


def store_new(
    conn: sqlite3.Connection,
    site_id: int,
    candidates: list[Candidate],
    blocked: set[str],
    *,
    limits: Limits,
    now: str,
) -> tuple[int, int, int]:
    """Insert unseen candidates as new, then queue up to max_new_per_poll waiting ones.

    Posts over the cap stay `discovered` and are queued by a later poll, so a 304 on the
    next poll cannot lose them. A site limit replaces that: the first limit unseen,
    unblocked candidates in source order are queued and the rest skipped as over_limit.
    """
    if limits.limit is not None:
        return _store_new_limited(conn, site_id, candidates, blocked, limit=limits.limit, now=now)
    new = skipped = 0
    for item in reversed(_newest_first(candidates)):
        post_id = posts.insert_post(conn, site_id, item, "new", now)
        if post_id is None:
            continue
        new += 1
        if item.url in blocked:
            posts.skip_post(conn, post_id, "robots", now)
            skipped += 1
    waiting = posts.waiting_new(conn, site_id, limits.max_new_per_poll)
    for post_id in waiting:
        posts.queue_post(conn, post_id, now)
    assert len(waiting) <= limits.max_new_per_poll
    return new, len(waiting), skipped


def _store_new_limited(
    conn: sqlite3.Connection,
    site_id: int,
    candidates: list[Candidate],
    blocked: set[str],
    *,
    limit: int,
    now: str,
) -> tuple[int, int, int]:
    new = queued = skipped = 0
    for item in candidates:
        post_id = posts.insert_post(conn, site_id, item, "new", now)
        if post_id is None:
            continue
        new += 1
        reason = "robots" if item.url in blocked else "over_limit" if queued >= limit else None
        if reason is None:
            posts.queue_post(conn, post_id, now)
            queued += 1
        else:
            posts.skip_post(conn, post_id, reason, now)
            skipped += 1
    assert queued + skipped == new
    assert queued <= limit
    return new, queued, skipped
