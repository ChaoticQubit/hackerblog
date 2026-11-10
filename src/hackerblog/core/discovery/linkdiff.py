"""Link extraction and the post URL heuristic for link-diff discovery (section 8.3)."""

import re
from collections.abc import Sequence
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from hackerblog.core.discovery.feeds import decode_html
from hackerblog.core.discovery.model import URL_LENGTH_MAX
from hackerblog.core.net.urls import canonicalize

MAX_PAGE_LINKS = 10000
SLUG_LENGTH_MIN = 12
DEPTH_MIN = 2
YEAR_SEGMENT = re.compile(r"(19|20)[0-9]{2}")
EXCLUDED_SEGMENTS = frozenset(
    {
        "tag",
        "tags",
        "category",
        "categories",
        "author",
        "page",
        "feed",
        "search",
        "about",
        "contact",
    }
)
EXCLUDED_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".avif",
    ".bmp",
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".bz2",
    ".xz",
    ".7z",
    ".rar",
    ".pdf",
    ".css",
    ".js",
)


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a" or len(self.hrefs) == MAX_PAGE_LINKS:
            return
        href = dict(attrs).get("href")
        if href and href.strip():
            self.hrefs.append(href.strip())


def extract_links(html: bytes, base_url: str) -> list[str]:
    """Return canonical absolute http(s) links from <a href> in the page, deduped, in order.

    Links that do not parse or are longer than URL_LENGTH_MAX are dropped; at most
    MAX_PAGE_LINKS anchors are read.
    """
    assert base_url.startswith(("http://", "https://")), base_url
    parser = _AnchorParser()
    parser.feed(decode_html(html))
    parser.close()
    links: dict[str, None] = {}
    for href in parser.hrefs:
        try:
            link = canonicalize(urljoin(base_url, href))
        except ValueError:
            continue
        if len(link) <= URL_LENGTH_MAX:
            links[link] = None
    assert len(links) <= MAX_PAGE_LINKS
    return list(links)


def _same_host(url: str, site_url: str) -> bool:
    try:
        target = urlsplit(url)
        source = urlsplit(site_url)
    except ValueError:
        return False
    if target.scheme not in {"http", "https"}:
        return False
    host = (target.hostname or "").lower()
    return bool(host) and host == (source.hostname or "").lower()


def _looks_like_post(path: str, site_path: str, roots: Sequence[str]) -> bool:
    segments = [segment for segment in path.lower().split("/") if segment]
    if not segments:
        return False
    if EXCLUDED_SEGMENTS.intersection(segments):
        return False
    if segments[-1].endswith(EXCLUDED_EXTENSIONS):
        return False
    dated = any(YEAR_SEGMENT.fullmatch(segment) for segment in segments[:-1])
    if path.startswith(site_path.rstrip("/") + "/") or dated:
        depth = len(segments)
    else:
        under = [root for root in roots if path.startswith(root)]
        if not under:
            return False
        depth = len(segments) - max(root.count("/") - 1 for root in under)
    assert depth <= len(segments)
    if depth < 1:
        return False
    return depth >= DEPTH_MIN or len(segments[-1]) >= SLUG_LENGTH_MIN


def post_roots(site_url: str, urls: Sequence[str]) -> tuple[str, ...]:
    """Return the parent directories of the site's own post URLs on its exact host.

    A feed tells where posts live; a Bear blog's /blog/ index links posts at /<slug>/,
    outside the site path, so those parents widen the heuristic for the sitemap and index.
    """
    host = (urlsplit(site_url).hostname or "").lower()
    assert host, site_url
    found: dict[str, None] = {}
    for url in urls:
        parts = urlsplit(url)
        if (parts.hostname or "").lower() == host:
            found[parts.path.rstrip("/").rsplit("/", 1)[0] + "/"] = None
    assert len(found) <= len(urls)
    return tuple(found)


def is_post_url(url: str, site_url: str, pattern: str | None, roots: Sequence[str] = ()) -> bool:
    """Tell whether url is a post of the site at site_url.

    The site page itself and the host's home page never are. With a pattern: same host and
    re.search(pattern, url). Without: the section 8.3 post heuristic (same host, under the
    site path, a root from post_roots, or dated, deep or long enough, not excluded).
    """
    assert site_url.startswith(("http://", "https://")), site_url
    assert all(root.startswith("/") and root.endswith("/") for root in roots), roots
    if not _same_host(url, site_url):
        return False
    path = urlsplit(url).path.rstrip("/")
    site_path = urlsplit(site_url).path.rstrip("/")
    if path in {"", site_path}:
        return False
    if pattern is not None:
        return re.search(pattern, url) is not None
    return _looks_like_post(urlsplit(url).path, site_path, roots)
