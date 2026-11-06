"""Link extraction and the post URL heuristic for link-diff discovery (section 8.3)."""

import re
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


def _looks_like_post(path: str, site_path: str) -> bool:
    if path.rstrip("/") == site_path.rstrip("/"):
        return False
    segments = [segment for segment in path.lower().split("/") if segment]
    if not segments:
        return False
    if EXCLUDED_SEGMENTS.intersection(segments):
        return False
    if segments[-1].endswith(EXCLUDED_EXTENSIONS):
        return False
    prefix = site_path.rstrip("/") + "/"
    dated = any(YEAR_SEGMENT.fullmatch(segment) for segment in segments[:-1])
    if not (path.startswith(prefix) or dated):
        return False
    return len(segments) >= DEPTH_MIN or len(segments[-1]) >= SLUG_LENGTH_MIN


def is_post_url(url: str, site_url: str, pattern: str | None) -> bool:
    """Tell whether url is a post of the site at site_url.

    With a pattern: same host and re.search(pattern, url). Without: the section 8.3 post
    heuristic (same host, under the site path or dated, deep or long enough, not excluded).
    """
    assert site_url.startswith(("http://", "https://")), site_url
    if not _same_host(url, site_url):
        return False
    if pattern is not None:
        return re.search(pattern, url) is not None
    return _looks_like_post(urlsplit(url).path, urlsplit(site_url).path)
