"""A real local HTTP server with three fixture blogs for discovery integration tests.

feedblog has an RSS feed with the newest 3 posts and a sitemap with all of them, mapblog
has only a sitemap index, plainblog has only an index page. Every site resource carries a
strong ETag and Last-Modified and answers conditional requests with 304.
"""

import hashlib
import threading
import zlib
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime, parsedate_to_datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import parse_qs, urlsplit

SITES = ("feedblog", "mapblog", "plainblog")
BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)
FEED_ITEMS_MAX = 3
BIG_BODY_SIZE = 6 * 1024 * 1024
BOMB_CHUNK_SIZE = 1024 * 1024
BOMB_CHUNK_COUNT = 1024
SLOW_SECONDS_MAX = 120.0
WRITE_CHUNK_SIZE = 64 * 1024
HTML = "text/html; charset=utf-8"
XML = "application/xml"
RSS = "application/rss+xml"
FILLER = (
    "Discovery reads feeds, sitemaps and index pages, keeps only the links that look like "
    "posts, and stores each canonical URL once so that a second poll finds nothing new. "
)
ROBOTS = "User-agent: *\nDisallow: /plainblog/private/\n"
INITIAL_POSTS = {
    "feedblog": [
        ("intro-to-xml-external-entities", "Intro to XML external entities"),
        ("ssrf-through-pdf-renderers", "SSRF through PDF renderers"),
        ("cache-poisoning-basics", "Cache poisoning basics"),
        ("request-smuggling-revisited", "Request smuggling revisited"),
        ("oauth-redirect-pitfalls", "OAuth redirect pitfalls"),
        ("prototype-pollution-in-node", "Prototype pollution in Node"),
        ("dns-rebinding-explained", "DNS rebinding explained"),
        ("csp-bypasses-in-practice", "CSP bypasses in practice"),
    ],
    "mapblog": [
        ("building-a-tiny-tcp-stack", "Building a tiny TCP stack"),
        ("why-sqlite-wal-mode-matters", "Why SQLite WAL mode matters"),
        ("reading-tls-handshakes-by-hand", "Reading TLS handshakes by hand"),
        ("bloom-filters-for-url-dedup", "Bloom filters for URL dedup"),
        ("rate-limiting-with-token-buckets", "Rate limiting with token buckets"),
        ("parsing-sitemaps-without-tears", "Parsing sitemaps without tears"),
    ],
    "plainblog": [
        ("hello-plain-html-world", "Hello plain HTML world"),
        ("notes-on-static-site-hosting", "Notes on static site hosting"),
        ("a-week-with-a-mechanical-keyboard", "A week with a mechanical keyboard"),
        ("learning-rust-the-slow-way", "Learning Rust the slow way"),
        ("what-i-read-this-winter", "What I read this winter"),
    ],
}
PLAIN_NAV = (
    "/plainblog/about/",
    "/plainblog/tags/",
    "/plainblog/page/2/",
    "https://example.com/elsewhere-on-the-web",
    "/plainblog/images/header-banner.png",
)


@dataclass(frozen=True)
class Post:
    """One fixture post: slug, title and publication time (UTC)."""

    slug: str
    title: str
    published: datetime


@dataclass(frozen=True)
class Resource:
    """One served document with its validators."""

    body: bytes
    content_type: str
    etag: str
    last_modified: datetime


Pages = dict[str, tuple[str, str]]


def post_path(site: str, slug: str) -> str:
    """Return the path of a post on a fixture site."""
    assert site in SITES, site
    assert slug and "/" not in slug, slug
    if site == "mapblog":
        return f"/mapblog/posts/{slug}/"
    return f"/{site}/2026/{slug}/"


def _page(base_url: str, path: str, title: str, body_html: str, head: str = "") -> str:
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        f"<title>{escape(title)}</title>\n"
        f'<link rel="canonical" href="{base_url}{path}">\n{head}</head>\n'
        f"<body>\n{body_html}\n</body>\n</html>\n"
    )


def _article(base_url: str, site: str, post: Post) -> str:
    paragraph = f"<p>{escape(post.title)}. {FILLER * 4}</p>"
    body = f"<article>\n<h1>{escape(post.title)}</h1>\n{paragraph * 3}\n</article>"
    return _page(base_url, post_path(site, post.slug), post.title, body)


def _link_list(base_url: str, site: str, posts: list[Post]) -> str:
    items = "".join(
        f'<li><a href="{base_url}{post_path(site, post.slug)}">{escape(post.title)}</a></li>\n'
        for post in reversed(posts)
    )
    return f"<ul>\n{items}</ul>"


def _urlset(entries: list[tuple[str, datetime | None]]) -> str:
    urls = "".join(
        f"  <url><loc>{escape(loc)}</loc>"
        + (f"<lastmod>{when.strftime('%Y-%m-%dT%H:%M:%SZ')}</lastmod>" if when else "")
        + "</url>\n"
        for loc, when in entries
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{urls}</urlset>\n'
    )


def _rss(base_url: str, posts: list[Post]) -> str:
    items = "".join(
        f"<item><title>{escape(post.title)}</title>"
        f"<link>{base_url}{post_path('feedblog', post.slug)}</link>"
        f'<guid isPermaLink="true">{base_url}{post_path("feedblog", post.slug)}</guid>'
        f"<pubDate>{format_datetime(post.published, usegmt=True)}</pubDate></item>\n"
        for post in reversed(posts[-FEED_ITEMS_MAX:])
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel>\n'
        f"<title>Feed Blog</title><link>{base_url}/feedblog/</link>\n"
        f"<description>Fixture blog with a feed</description>\n{items}</channel></rss>\n"
    )


def _feedblog(base_url: str, posts: list[Post]) -> Pages:
    feed_link = (
        '<link rel="alternate" type="application/rss+xml" title="RSS" href="/feedblog/feed.xml">\n'
    )
    home = _page(
        base_url,
        "/feedblog/",
        "Feed Blog",
        f"<h1>Feed Blog</h1>\n{_link_list(base_url, 'feedblog', posts)}",
        feed_link,
    )
    entries: list[tuple[str, datetime | None]] = [
        (f"{base_url}{post_path('feedblog', p.slug)}", p.published) for p in posts
    ]
    return {
        "/feedblog/": (home, HTML),
        "/feedblog/feed.xml": (_rss(base_url, posts), RSS),
        "/feedblog/sitemap.xml": (_urlset(entries), XML),
    }


def _mapblog(base_url: str, posts: list[Post]) -> Pages:
    archive, recent = posts[:2], posts[2:]
    archive_entries: list[tuple[str, datetime | None]] = [
        (f"{base_url}{post_path('mapblog', p.slug)}", p.published) for p in archive
    ]
    archive_entries.append((f"{base_url}/mapblog/tag/networking/", None))
    recent_entries: list[tuple[str, datetime | None]] = [
        (f"{base_url}/mapblog/", None),
        (f"{base_url}/mapblog/about/", None),
    ]
    recent_entries += [(f"{base_url}{post_path('mapblog', p.slug)}", p.published) for p in recent]
    children = "".join(
        f"  <sitemap><loc>{base_url}/mapblog/{name}</loc>"
        f"<lastmod>{max(p.published for p in group).strftime('%Y-%m-%dT%H:%M:%SZ')}</lastmod>"
        "</sitemap>\n"
        for name, group in (("sitemap-archive.xml", archive), ("sitemap-posts.xml", recent))
    )
    index = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{children}</sitemapindex>\n"
    )
    home = f"<h1>Map Blog</h1>\n{_link_list(base_url, 'mapblog', posts)}"
    about = "<h1>About</h1>\n<p>A blog with only a sitemap.</p>"
    tag = f"<h1>Networking</h1>\n{_link_list(base_url, 'mapblog', posts)}"
    return {
        "/mapblog/": (_page(base_url, "/mapblog/", "Map Blog", home), HTML),
        "/mapblog/about/": (_page(base_url, "/mapblog/about/", "About", about), HTML),
        "/mapblog/tag/networking/": (
            _page(base_url, "/mapblog/tag/networking/", "Networking", tag),
            HTML,
        ),
        "/mapblog/sitemap.xml": (index, XML),
        "/mapblog/sitemap-archive.xml": (_urlset(archive_entries), XML),
        "/mapblog/sitemap-posts.xml": (_urlset(recent_entries), XML),
    }


def _plainblog(base_url: str, posts: list[Post]) -> Pages:
    nav = " ".join(
        f'<a href="{href}">{href.rstrip("/").rsplit("/", 1)[-1]}</a>' for href in PLAIN_NAV
    )
    home = f"<nav>{nav}</nav>\n<h1>Plain Blog</h1>\n{_link_list(base_url, 'plainblog', posts)}"
    pages: Pages = {"/plainblog/": (_page(base_url, "/plainblog/", "Plain Blog", home), HTML)}
    for path in (*PLAIN_NAV[:3], "/plainblog/private/"):
        pages[path] = (_page(base_url, path, path, f"<h1>{escape(path)}</h1>"), HTML)
    return pages


def build_site(base_url: str, site: str, posts: list[Post]) -> Pages:
    """Return every path of one fixture site mapped to (text body, content type)."""
    assert site in SITES, site
    assert posts, site
    builders = {"feedblog": _feedblog, "mapblog": _mapblog, "plainblog": _plainblog}
    pages = builders[site](base_url, posts)
    for post in posts:
        pages[post_path(site, post.slug)] = (_article(base_url, site, post), HTML)
    return pages


_bomb_lock = threading.Lock()
_bomb: list[bytes] = []


def gzip_bomb() -> bytes:
    """Return gzip bytes of 1 GiB of zeros, compressed once per process (about 1 MB)."""
    with _bomb_lock:
        if not _bomb:
            compressor = zlib.compressobj(9, zlib.DEFLATED, 31)
            zeros = bytes(BOMB_CHUNK_SIZE)
            parts = [compressor.compress(zeros) for _ in range(BOMB_CHUNK_COUNT)]
            parts.append(compressor.flush())
            _bomb.append(b"".join(parts))
        assert len(_bomb) == 1
        return _bomb[0]


class FixtureServer:
    """Serve the fixture blogs on 127.0.0.1 with a random port in a daemon thread.

    requests lists (method, path, status) per request; publish adds a post at runtime.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: list[tuple[str, str, int]] = []
        self._resources: dict[str, Resource] = {}
        self._generation = 0
        self._breaks: dict[str, int] = {}
        self.closing = threading.Event()
        self._httpd = _Server(("127.0.0.1", 0), _Handler)
        self._httpd.fixture = self
        port = self._httpd.server_address[1]
        assert isinstance(port, int)
        self.base_url = f"http://127.0.0.1:{port}"
        self._posts = {
            site: [
                Post(slug, title, BASE_TIME + timedelta(days=index))
                for index, (slug, title) in enumerate(entries)
            ]
            for site, entries in INITIAL_POSTS.items()
        }
        self._store({"/robots.txt": (ROBOTS, "text/plain; charset=utf-8")})
        for site in SITES:
            self._store(build_site(self.base_url, site, self._posts[site]))
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def requests(self) -> list[tuple[str, str, int]]:
        """Return a copy of the (method, path, status) log."""
        with self._lock:
            return list(self._requests)

    def reset_counts(self) -> None:
        """Forget every logged request."""
        with self._lock:
            self._requests.clear()

    def record(self, method: str, path: str, status: int) -> None:
        """Log one answered request; called from handler threads."""
        assert 100 <= status < 600, status
        with self._lock:
            self._requests.append((method, path, status))

    def break_after(self, path: str, successes: int) -> None:
        """Answer 500 for path once it has been served successes more times, until heal."""
        assert successes >= 0
        with self._lock:
            self._breaks[path] = successes

    def heal(self, path: str) -> None:
        """Serve path normally again."""
        with self._lock:
            self._breaks.pop(path, None)

    def broken(self, path: str) -> bool:
        """Return whether this request for path must fail, counting down its successes."""
        with self._lock:
            left = self._breaks.get(path)
            if left is None:
                return False
            if left == 0:
                return True
            self._breaks[path] = left - 1
            return False

    def resource(self, path: str) -> Resource | None:
        """Return the resource served at path, if any."""
        with self._lock:
            return self._resources.get(path)

    def post_urls(self, site: str) -> list[str]:
        """Return the absolute URLs of every post of a site, oldest first."""
        with self._lock:
            return [f"{self.base_url}{post_path(site, p.slug)}" for p in self._posts[site]]

    def publish(self, site: str, slug: str, title: str) -> str:
        """Add a new newest post to a site, rebuild its documents and return the post URL.

        Changed documents get a new ETag and a later Last-Modified; unchanged ones keep theirs.
        """
        assert site in SITES, site
        with self._lock:
            posts = self._posts[site]
            assert all(post.slug != slug for post in posts), slug
            posts.append(Post(slug, title, posts[-1].published + timedelta(days=1)))
            self._generation += 1
            pages = build_site(self.base_url, site, posts)
        self._store(pages)
        return f"{self.base_url}{post_path(site, slug)}"

    def _store(self, pages: Pages) -> None:
        with self._lock:
            modified = BASE_TIME + timedelta(hours=self._generation)
            for path, (text, content_type) in pages.items():
                body = text.encode("utf-8")
                current = self._resources.get(path)
                if current is not None and current.body == body:
                    continue
                etag = f'"{hashlib.sha256(body).hexdigest()[:32]}"'
                self._resources[path] = Resource(body, content_type, etag, modified)

    def close(self) -> None:
        """Stop serving and wait for the server thread."""
        self.closing.set()
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=10)
        assert not self._thread.is_alive()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    fixture: FixtureServer


def _not_modified(request_headers: dict[str, str], resource: Resource) -> bool:
    if_none_match = request_headers.get("if-none-match")
    if if_none_match is not None:
        tags = [tag.strip().removeprefix("W/") for tag in if_none_match.split(",")]
        return "*" in tags or resource.etag in tags
    if_modified_since = request_headers.get("if-modified-since")
    if if_modified_since is None:
        return False
    try:
        since = parsedate_to_datetime(if_modified_since)
    except (TypeError, ValueError):
        return False
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    return resource.last_modified <= since


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def fixture(self) -> FixtureServer:
        return cast(_Server, self.server).fixture

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        method = getattr(self, "command", None) or "-"
        path = getattr(self, "path", None) or "-"
        self.fixture.record(method, path, int(code))

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_HEAD(self) -> None:
        self._dispatch()

    def do_GET(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        path = urlsplit(self.path).path
        special = {
            "/redirect-to": self._redirect,
            "/slow": self._slow,
            "/big": self._big,
            "/gzip-bomb": self._gzip_bomb,
            "/retry": self._retry,
        }
        if path in special:
            special[path]()
            return
        if self.fixture.broken(path):
            self._send(500, b"broken\n", "text/plain; charset=utf-8")
            return
        resource = self.fixture.resource(path)
        if resource is None:
            self._send(404, b"not found\n", "text/plain; charset=utf-8")
            return
        headers = {key.lower(): value for key, value in self.headers.items()}
        validators = {
            "ETag": resource.etag,
            "Last-Modified": format_datetime(resource.last_modified, usegmt=True),
        }
        if _not_modified(headers, resource):
            self.send_response(304)
            for key, value in validators.items():
                self.send_header(key, value)
            self.end_headers()
            return
        self._send(200, resource.body, resource.content_type, validators)

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self._write(body)

    def _write(self, body: bytes) -> None:
        if self.command == "HEAD":
            return
        try:
            for start in range(0, len(body), WRITE_CHUNK_SIZE):
                self.wfile.write(body[start : start + WRITE_CHUNK_SIZE])
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _redirect(self) -> None:
        targets = parse_qs(urlsplit(self.path).query).get("url", [])
        if len(targets) != 1:
            self._send(400, b"need exactly one url parameter\n", "text/plain; charset=utf-8")
            return
        self._send(302, b"", "text/plain; charset=utf-8", {"Location": targets[0]})

    def _slow(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", "5")
        self.end_headers()
        if self.fixture.closing.wait(SLOW_SECONDS_MAX):
            self.close_connection = True
            return
        self._write(b"done\n")

    def _big(self) -> None:
        self._send(200, b"x" * BIG_BODY_SIZE, HTML)

    def _gzip_bomb(self) -> None:
        self._send(200, gzip_bomb(), XML, {"Content-Encoding": "gzip"})

    def _retry(self) -> None:
        self._send(429, b"slow down\n", "text/plain; charset=utf-8", {"Retry-After": "120"})


@contextmanager
def serve_blogs() -> Generator[FixtureServer]:
    """Run a FixtureServer for the duration of a with block and close it afterwards."""
    server = FixtureServer()
    try:
        yield server
    finally:
        server.close()
