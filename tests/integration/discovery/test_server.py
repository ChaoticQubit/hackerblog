import http.client
import socket
import time
import zlib
from collections.abc import Iterator
from urllib.parse import urlsplit

import pytest

from hackerblog.core.discovery.feeds import feed_links, page_title, parse_feed
from hackerblog.core.discovery.linkdiff import extract_links, is_post_url
from hackerblog.core.discovery.sitemaps import parse_sitemap

from .server import BIG_BODY_SIZE, FixtureServer, serve_blogs

Response = tuple[int, dict[str, str], bytes]


@pytest.fixture(scope="module")
def blogs() -> Iterator[FixtureServer]:
    with serve_blogs() as server:
        yield server


def get(base_url: str, path: str, headers: dict[str, str] | None = None) -> Response:
    parts = urlsplit(base_url)
    assert parts.hostname is not None
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    try:
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, body
    finally:
        connection.close()


def posts_in(urls: list[str], site_url: str) -> list[str]:
    return [url for url in urls if is_post_url(url, site_url, None)]


def test_feedblog_feed_has_newest_three_and_sitemap_has_all(blogs: FixtureServer) -> None:
    site = f"{blogs.base_url}/feedblog/"
    status, _, home = get(blogs.base_url, "/feedblog/")
    assert status == 200
    assert feed_links(home, site) == [f"{blogs.base_url}/feedblog/feed.xml"]
    status, _, feed = get(blogs.base_url, "/feedblog/feed.xml")
    assert status == 200
    candidates = parse_feed(feed, site)
    assert candidates is not None
    all_posts = blogs.post_urls("feedblog")
    assert len(all_posts) == 8
    assert [c.url for c in candidates] == list(reversed(all_posts))[:3]
    assert all(c.published_at for c in candidates)
    status, _, sitemap = get(blogs.base_url, "/feedblog/sitemap.xml")
    assert status == 200
    pages = parse_sitemap(sitemap).pages
    assert [loc for loc, _ in pages] == all_posts
    assert posts_in([loc for loc, _ in pages], site) == all_posts


def test_mapblog_has_only_a_sitemap_index(blogs: FixtureServer) -> None:
    site = f"{blogs.base_url}/mapblog/"
    _, _, home = get(blogs.base_url, "/mapblog/")
    assert feed_links(home, site) == []
    for path in ("/feed", "/rss.xml", "/atom.xml", "/index.xml", "/feed.xml", "/mapblog/feed"):
        assert get(blogs.base_url, path)[0] == 404
    status, _, index = get(blogs.base_url, "/mapblog/sitemap.xml")
    assert status == 200
    children = parse_sitemap(index).children
    assert len(children) == 2
    locs: list[str] = []
    for child in children:
        status, _, body = get(blogs.base_url, urlsplit(child).path)
        assert status == 200
        locs += [loc for loc, _ in parse_sitemap(body).pages]
    assert len(locs) == 9
    assert sorted(posts_in(locs, site)) == sorted(blogs.post_urls("mapblog"))
    assert len(blogs.post_urls("mapblog")) == 6


def test_plainblog_index_links_five_posts(blogs: FixtureServer) -> None:
    site = f"{blogs.base_url}/plainblog/"
    assert get(blogs.base_url, "/plainblog/sitemap.xml")[0] == 404
    assert get(blogs.base_url, "/sitemap.xml")[0] == 404
    status, _, index = get(blogs.base_url, "/plainblog/")
    assert status == 200
    assert feed_links(index, site) == []
    links = extract_links(index, site)
    posts = posts_in(links, site)
    assert len(posts) == 5
    assert sorted(posts) == sorted(blogs.post_urls("plainblog"))
    assert "https://example.com/elsewhere-on-the-web" in links
    assert len(links) > len(posts)


def test_post_pages_are_articles_with_canonical(blogs: FixtureServer) -> None:
    for site in ("feedblog", "mapblog", "plainblog"):
        url = blogs.post_urls(site)[0]
        status, headers, body = get(blogs.base_url, urlsplit(url).path)
        assert status == 200
        assert headers["content-type"].startswith("text/html")
        text = body.decode()
        assert f'<link rel="canonical" href="{url}">' in text
        assert page_title(body)
        assert len(text.split()) >= 200


def test_robots_allows_all_but_private(blogs: FixtureServer) -> None:
    status, _, body = get(blogs.base_url, "/robots.txt")
    assert status == 200
    assert body == b"User-agent: *\nDisallow: /plainblog/private/\n"
    assert b"Sitemap" not in body
    assert get(blogs.base_url, "/plainblog/private/")[0] == 200


def test_conditional_requests_get_304(blogs: FixtureServer) -> None:
    status, headers, _ = get(blogs.base_url, "/feedblog/feed.xml")
    assert status == 200
    etag = headers["etag"]
    assert etag.startswith('"')
    assert not etag.startswith("W/")
    status, again, body = get(blogs.base_url, "/feedblog/feed.xml", {"If-None-Match": etag})
    assert (status, body) == (304, b"")
    assert again["etag"] == etag
    since = {"If-Modified-Since": headers["last-modified"]}
    assert get(blogs.base_url, "/feedblog/feed.xml", since)[0] == 304
    stale = {"If-None-Match": '"nope"', "If-Modified-Since": headers["last-modified"]}
    assert get(blogs.base_url, "/feedblog/feed.xml", stale)[0] == 200
    assert get(blogs.base_url, "/missing")[0] == 404


def test_request_counting(blogs: FixtureServer) -> None:
    blogs.reset_counts()
    get(blogs.base_url, "/mapblog/")
    get(blogs.base_url, "/nothing-here")
    assert blogs.requests == [("GET", "/mapblog/", 200), ("GET", "/nothing-here", 404)]
    blogs.reset_counts()
    assert blogs.requests == []


@pytest.mark.parametrize(
    ("site", "documents"),
    [
        ("feedblog", ["/feedblog/", "/feedblog/feed.xml", "/feedblog/sitemap.xml"]),
        ("mapblog", ["/mapblog/sitemap.xml", "/mapblog/sitemap-posts.xml"]),
        ("plainblog", ["/plainblog/"]),
    ],
)
def test_publish_changes_documents_and_validators(site: str, documents: list[str]) -> None:
    with serve_blogs() as server:
        before = {path: get(server.base_url, path)[1] for path in documents}
        unchanged = get(server.base_url, "/robots.txt")[1]["etag"]
        url = server.publish(site, "a-freshly-published-post", "A freshly published post")
        assert server.post_urls(site)[-1] == url
        for path, headers in before.items():
            status, after, body = get(server.base_url, path, {"If-None-Match": headers["etag"]})
            assert status == 200, path
            assert after["etag"] != headers["etag"]
            assert after["last-modified"] != headers["last-modified"]
            assert url.encode() in body or path.endswith("/sitemap.xml")
        assert get(server.base_url, "/robots.txt")[1]["etag"] == unchanged
        assert get(server.base_url, urlsplit(url).path)[0] == 200


def test_publish_puts_new_post_first_in_feed() -> None:
    with serve_blogs() as server:
        url = server.publish("feedblog", "breaking-news-for-feeds", "Breaking news for feeds")
        site = f"{server.base_url}/feedblog/"
        candidates = parse_feed(get(server.base_url, "/feedblog/feed.xml")[2], site)
        assert candidates is not None
        assert candidates[0].url == url
        assert len(candidates) == 3


def test_redirect_retry_and_big(blogs: FixtureServer) -> None:
    status, headers, _ = get(blogs.base_url, "/redirect-to?url=http://127.0.0.1:1/x")
    assert (status, headers["location"]) == (302, "http://127.0.0.1:1/x")
    status, headers, _ = get(blogs.base_url, "/retry")
    assert (status, headers["retry-after"]) == (429, "120")
    status, _, body = get(blogs.base_url, "/big")
    assert (status, len(body)) == (200, BIG_BODY_SIZE)


def test_gzip_bomb_is_small_on_the_wire_and_huge_decoded(blogs: FixtureServer) -> None:
    status, headers, body = get(blogs.base_url, "/gzip-bomb")
    assert status == 200
    assert headers["content-encoding"] == "gzip"
    assert len(body) < 2 * 1024 * 1024
    decoder = zlib.decompressobj(zlib.MAX_WBITS | 16)
    head = decoder.decompress(body, 16 * 1024 * 1024)
    assert len(head) == 16 * 1024 * 1024
    assert head.count(0) == len(head)
    assert decoder.unconsumed_tail


def test_slow_does_not_answer_quickly(blogs: FixtureServer) -> None:
    parts = urlsplit(blogs.base_url)
    assert parts.hostname is not None
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=1)
    started = time.monotonic()
    try:
        connection.request("GET", "/slow")
        response = connection.getresponse()
        assert response.status == 200
        with pytest.raises(TimeoutError):
            response.read()
    finally:
        connection.close()
    assert time.monotonic() - started < 5


def test_malformed_request_line_gets_400_and_is_logged(blogs: FixtureServer) -> None:
    blogs.reset_counts()
    port = urlsplit(blogs.base_url).port
    assert port is not None
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        connection.sendall(b"NOT A VALID REQUEST LINE AT ALL\r\n\r\n")
        reply = connection.recv(1024)
    assert reply.startswith(b"HTTP/1.")
    assert b" 400 " in reply.split(b"\r\n", 1)[0]
    assert [status for _, _, status in blogs.requests] == [400]
