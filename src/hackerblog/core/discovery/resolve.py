"""Pick how a site's posts are found: feed, then sitemap, then link diff (section 8.2)."""

from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from hackerblog.core.config import SiteConfig
from hackerblog.core.discovery.feeds import COMMON_FEED_PATHS, feed_links, page_title, parse_feed
from hackerblog.core.discovery.sitemaps import parse_sitemap
from hackerblog.core.net.fetcher import Fetcher, FetchError
from hackerblog.core.net.guard import BlockedError
from hackerblog.core.store.sites import Method

FEED_PROBES_MAX = 8
SITEMAP_PROBES_MAX = 4


@dataclass(frozen=True)
class Resolution:
    """The chosen method, the feed and sitemap found, and the home page title."""

    method: Method
    feed_url: str | None
    sitemap_url: str | None
    title: str | None

    def __post_init__(self) -> None:
        assert self.method != "feed" or self.feed_url is not None
        assert self.method != "sitemap" or self.sitemap_url is not None


async def resolve(fetcher: Fetcher, site: SiteConfig) -> Resolution:
    """Resolve a site's sources over the network.

    A configured feed or sitemap is trusted without probing. Raises FetchError when the
    home page is unreachable and BlockedError when the guard or robots refuses it.
    """
    title: str | None = None
    feed = site.feed
    if feed is None:
        home = await fetcher.get(site.url, kind="page")
        if home.status != 200:
            raise FetchError(f"{site.url} answered HTTP {home.status}", status=home.status)
        title = page_title(home.body)
        probes = [*feed_links(home.body, home.url), *_common_feeds(site.url)]
        feed = await _first_feed(fetcher, _unique(probes)[:FEED_PROBES_MAX])
    sitemap = site.sitemap or await _find_sitemap(fetcher, site.url)
    method: Method = "feed" if feed else ("sitemap" if sitemap else "linkdiff")
    return Resolution(method, feed, sitemap, title)


def _unique(urls: list[str]) -> list[str]:
    seen: dict[str, None] = dict.fromkeys(urls)
    assert len(seen) <= len(urls)
    return list(seen)


def _common_feeds(site_url: str) -> list[str]:
    base = site_url if site_url.endswith("/") else site_url + "/"
    found = [urljoin(base, path.lstrip("/")) for path in COMMON_FEED_PATHS]
    assert len(found) == len(COMMON_FEED_PATHS)
    return found


async def _first_feed(fetcher: Fetcher, probes: list[str]) -> str | None:
    """Return the first probe that answers with a feed holding at least one linked entry.

    A probe refused by robots, the URL policy or the SSRF guard is skipped, so a hostile
    feed link on the home page cannot fail the whole site.
    """
    assert len(probes) <= FEED_PROBES_MAX
    for url in probes:
        try:
            response = await fetcher.get(url, kind="feed")
        except (BlockedError, FetchError):
            continue
        if response.status == 200 and parse_feed(response.body, response.url):
            return response.url
    return None


async def _find_sitemap(fetcher: Fetcher, site_url: str) -> str | None:
    """Return the robots Sitemap line, or a /sitemap.xml under the site or its origin."""
    host = urlsplit(site_url).hostname
    declared = await fetcher.robots_sitemaps(site_url)
    same_host = [url for url in declared if urlsplit(url).hostname == host]
    base = site_url if site_url.endswith("/") else site_url + "/"
    probes = _unique([*same_host, urljoin(base, "sitemap.xml"), urljoin(base, "/sitemap.xml")])
    for url in probes[:SITEMAP_PROBES_MAX]:
        try:
            response = await fetcher.get(url, kind="sitemap")
        except (BlockedError, FetchError):
            continue
        if response.status != 200:
            continue
        try:
            parsed = parse_sitemap(response.body)
        except ValueError:
            continue
        if parsed.pages or parsed.children:
            return response.url
    return None
