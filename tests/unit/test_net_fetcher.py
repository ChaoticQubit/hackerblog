import gzip
import re
import sqlite3
import threading
import time
import zlib
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

import hackerblog
from hackerblog.core.config import FetchConfig
from hackerblog.core.net.fetcher import Fetcher, FetchError
from hackerblog.core.net.guard import BlockedError
from hackerblog.core.net.robots import RobotsPolicy, store_policy
from hackerblog.core.store.migrate import connect, migrate

Reply = tuple[int, dict[str, str], bytes]
Route = Reply | Callable[[dict[str, str]], Reply]
MIB = 1024 * 1024


class FixtureServer:
    """A real HTTP server on 127.0.0.1 that answers from a route table and logs requests."""

    def __init__(self) -> None:
        self.routes: dict[str, Route] = {}
        self.seen: list[tuple[str, dict[str, str]]] = []
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()

    def url(self, path: str) -> str:
        return f"http://localhost:{self.port}{path}"

    def paths(self) -> list[str]:
        return [path for path, _ in self.seen]

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                headers = {key.lower(): value for key, value in self.headers.items()}
                server.seen.append((self.path, headers))
                route = server.routes.get(self.path, (404, {}, b"missing"))
                status, extra, body = route(headers) if callable(route) else route
                self.send_response(status)
                for key, value in extra.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    return

            def log_message(self, format: str, *args: object) -> None:
                return

        return Handler


async def resolve_localhost(host: str, port: int) -> Sequence[str]:
    assert host == "localhost"
    assert port > 0
    return ["127.0.0.1"]


@pytest.fixture
def server() -> Iterator[FixtureServer]:
    running = FixtureServer()
    yield running
    running.close()


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    database = connect(tmp_path / "db.sqlite")
    migrate(database)
    return database


CONFIG = FetchConfig(allow_private_hosts=["localhost"], min_delay_seconds=0.5)


def make_fetcher(
    conn: sqlite3.Connection, server: FixtureServer, *, robots: bool = False, persist: bool = True
) -> Fetcher:
    """Return a Fetcher for the fixture server; robots=False seeds an allow-all robots row."""
    if not robots:
        policy = RobotsPolicy("allow_all", None)
        store_policy(conn, f"http://localhost:{server.port}", policy, datetime.now(UTC))
    return Fetcher(conn, CONFIG, persist=persist, resolver=resolve_localhost)


def count(conn: sqlite3.Connection, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0])


@pytest.mark.anyio
async def test_get_page_sends_user_agent_and_lowercases_headers(
    conn: sqlite3.Connection, server: FixtureServer
) -> None:
    server.routes["/post"] = (200, {"X-Thing": "yes", "Content-Type": "text/html"}, b"<p>hi</p>")
    async with make_fetcher(conn, server) as fetcher:
        response = await fetcher.get(server.url("/post"), kind="page")
    assert response.status == 200
    assert response.body == b"<p>hi</p>"
    assert response.url == server.url("/post")
    assert response.headers["x-thing"] == "yes"
    assert not response.not_modified
    expected = f"HackerBlog/{hackerblog.__version__} (+https://github.com/hackerblog/hackerblog)"
    assert server.seen[0][1]["user-agent"] == expected


@pytest.mark.anyio
async def test_other_status_is_returned(conn: sqlite3.Connection, server: FixtureServer) -> None:
    async with make_fetcher(conn, server) as fetcher:
        response = await fetcher.get(server.url("/gone"), kind="page")
    assert response.status == 404
    assert response.body == b"missing"


@pytest.mark.anyio
async def test_redirect_is_followed_with_min_delay(
    conn: sqlite3.Connection, server: FixtureServer
) -> None:
    server.routes["/old"] = (301, {"Location": "/new"}, b"")
    server.routes["/new"] = (200, {}, b"moved")
    started_s = time.monotonic()
    async with make_fetcher(conn, server) as fetcher:
        response = await fetcher.get(server.url("/old"), kind="page")
    assert time.monotonic() - started_s >= CONFIG.min_delay_seconds
    assert response.url == server.url("/new")
    assert response.body == b"moved"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "target",
    [
        "http://127.0.0.1:{port}/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]:{port}/",
    ],
)
async def test_redirect_to_private_address_is_refused(
    conn: sqlite3.Connection, server: FixtureServer, target: str
) -> None:
    server.routes["/jump"] = (302, {"Location": target.format(port=server.port)}, b"")
    async with make_fetcher(conn, server) as fetcher:
        with pytest.raises(BlockedError) as caught:
            await fetcher.get(server.url("/jump"), kind="page")
    assert caught.value.reason == "policy"
    assert server.paths() == ["/jump"]


@pytest.mark.anyio
async def test_redirect_cap(conn: sqlite3.Connection, server: FixtureServer) -> None:
    for hop in range(6):
        server.routes[f"/r{hop}"] = (302, {"Location": f"/r{hop + 1}"}, b"")
    async with make_fetcher(conn, server) as fetcher:
        with pytest.raises(FetchError):
            await fetcher.get(server.url("/r0"), kind="page")
    assert len(server.seen) == 6


@pytest.mark.anyio
async def test_policy_and_ssrf_refusals(conn: sqlite3.Connection, server: FixtureServer) -> None:
    async def resolve_private(host: str, port: int) -> Sequence[str]:
        assert host == "blog.example"
        assert port == 80
        return ["93.184.216.34", "10.0.0.7"]

    async with make_fetcher(conn, server) as fetcher:
        with pytest.raises(BlockedError) as policy:
            await fetcher.get("ftp://blog.example/feed", kind="feed")
    assert policy.value.reason == "policy"
    async with Fetcher(conn, FetchConfig(), resolver=resolve_private) as fetcher:
        with pytest.raises(BlockedError) as ssrf:
            await fetcher.get("http://blog.example/feed", kind="feed")
    assert ssrf.value.reason == "ssrf"


@pytest.mark.anyio
async def test_gzip_bomb_is_cut(conn: sqlite3.Connection, server: FixtureServer) -> None:
    bomb = gzip.compress(b"\0" * (100 * MIB), compresslevel=9)
    assert len(bomb) < MIB
    server.routes["/bomb"] = (200, {"Content-Encoding": "gzip"}, bomb)
    async with make_fetcher(conn, server) as fetcher:
        with pytest.raises(FetchError) as caught:
            await fetcher.get(server.url("/bomb"), kind="page")
    assert caught.value.status is None
    assert "larger than" in caught.value.message


@pytest.mark.anyio
async def test_gzip_and_deflate_bodies_are_decoded(
    conn: sqlite3.Connection, server: FixtureServer
) -> None:
    server.routes["/gz"] = (200, {"Content-Encoding": "gzip"}, gzip.compress(b"gz body"))
    server.routes["/df"] = (200, {"Content-Encoding": "deflate"}, zlib.compress(b"df body"))
    async with make_fetcher(conn, server) as fetcher:
        assert (await fetcher.get(server.url("/gz"), kind="feed")).body == b"gz body"
        assert (await fetcher.get(server.url("/df"), kind="feed")).body == b"df body"


@pytest.mark.anyio
async def test_oversized_body_is_cut(conn: sqlite3.Connection, server: FixtureServer) -> None:
    server.routes["/big"] = (200, {}, b"x" * (5 * MIB + 1))
    async with make_fetcher(conn, server) as fetcher:
        with pytest.raises(FetchError):
            await fetcher.get(server.url("/big"), kind="page")


@pytest.mark.anyio
async def test_raw_gz_sitemap_is_gunzipped_under_cap(
    conn: sqlite3.Connection, server: FixtureServer
) -> None:
    xml = b"<urlset>" + b"<url><loc>https://blog.example/p</loc></url>" * 1000 + b"</urlset>"
    server.routes["/sitemap.xml.gz"] = (200, {}, gzip.compress(xml))
    server.routes["/bomb.xml.gz"] = (200, {}, gzip.compress(b" " * (21 * MIB)))
    async with make_fetcher(conn, server) as fetcher:
        response = await fetcher.get(server.url("/sitemap.xml.gz"), kind="sitemap")
        assert response.body == xml
        with pytest.raises(FetchError):
            await fetcher.get(server.url("/bomb.xml.gz"), kind="sitemap")


@pytest.mark.anyio
async def test_conditional_get_round_trip(conn: sqlite3.Connection, server: FixtureServer) -> None:
    etag = 'W/"v1"'
    modified = "Wed, 04 Nov 2026 10:00:00 GMT"

    def feed(headers: dict[str, str]) -> Reply:
        if headers.get("if-none-match") == etag:
            return (304, {}, b"")
        return (200, {"ETag": etag, "Last-Modified": modified}, b"<rss/>")

    server.routes["/feed"] = feed
    async with make_fetcher(conn, server) as fetcher:
        first = await fetcher.get(server.url("/feed"), kind="feed", conditional=True)
        assert count(conn, "SELECT count(*) FROM http_cache") == 0
        fetcher.remember(first)
        second = await fetcher.get(server.url("/feed"), kind="feed", conditional=True)
        fetcher.remember(second)
    assert first.status == 200
    assert first.body == b"<rss/>"
    assert "if-none-match" not in server.seen[0][1]
    assert server.seen[1][1]["if-none-match"] == etag
    assert server.seen[1][1]["if-modified-since"] == modified
    assert second.status == 304
    assert second.not_modified
    assert second.body == b""
    row = conn.execute("SELECT etag, last_modified, last_status FROM http_cache").fetchone()
    assert row == (etag, modified, 304)
    fetched_at = conn.execute("SELECT fetched_at FROM http_cache").fetchone()[0]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", fetched_at)


@pytest.mark.anyio
async def test_persist_false_writes_no_cache_rows(
    conn: sqlite3.Connection, server: FixtureServer
) -> None:
    server.routes["/feed"] = (200, {"ETag": '"x"'}, b"<rss/>")
    async with make_fetcher(conn, server, robots=True, persist=False) as fetcher:
        fetcher.remember(await fetcher.get(server.url("/feed"), kind="feed", conditional=True))
    assert count(conn, "SELECT count(*) FROM http_cache") == 0
    assert count(conn, "SELECT count(*) FROM robots_cache") == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "retry_after", "expected_s"),
    [(429, "120", 120.0), (503, "999999", 86400.0), (503, None, None)],
)
async def test_retry_statuses_raise_with_retry_after(
    conn: sqlite3.Connection,
    server: FixtureServer,
    status: int,
    retry_after: str | None,
    expected_s: float | None,
) -> None:
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    server.routes["/feed"] = (status, headers, b"slow down")
    async with make_fetcher(conn, server) as fetcher:
        with pytest.raises(FetchError) as caught:
            await fetcher.get(server.url("/feed"), kind="feed")
    assert caught.value.status == status
    assert caught.value.retry_after_s == expected_s


@pytest.mark.anyio
async def test_robots_4xx_allows_all_and_is_cached(
    conn: sqlite3.Connection, server: FixtureServer
) -> None:
    server.routes["/post"] = (200, {}, b"ok")
    async with make_fetcher(conn, server, robots=True) as fetcher:
        assert (await fetcher.get(server.url("/post"), kind="page")).status == 200
    assert server.paths() == ["/robots.txt", "/post"]
    assert conn.execute("SELECT policy FROM robots_cache").fetchone() == ("allow_all",)


@pytest.mark.anyio
async def test_robots_5xx_disallows_all_without_caching(
    conn: sqlite3.Connection, server: FixtureServer
) -> None:
    server.routes["/robots.txt"] = (500, {}, b"down")
    async with make_fetcher(conn, server, robots=True) as fetcher:
        with pytest.raises(BlockedError) as caught:
            await fetcher.get(server.url("/post"), kind="page")
    assert caught.value.reason == "robots"
    assert server.paths() == ["/robots.txt"]
    assert count(conn, "SELECT count(*) FROM robots_cache") == 0


@pytest.mark.anyio
async def test_robots_disallow_sitemaps_and_delay(
    conn: sqlite3.Connection, server: FixtureServer
) -> None:
    body = b"User-agent: *\nDisallow: /private\nSitemap: https://blog.example/s.xml\n"
    server.routes["/robots.txt"] = (200, {}, body)
    async with make_fetcher(conn, server, robots=True) as fetcher:
        assert not await fetcher.allowed(server.url("/private/x"))
        assert await fetcher.allowed(server.url("/public"))
        assert await fetcher.robots_sitemaps(server.url("/")) == ["https://blog.example/s.xml"]
        with pytest.raises(BlockedError) as caught:
            await fetcher.get(server.url("/private/x"), kind="page")
    assert caught.value.reason == "robots"
    assert server.paths() == ["/robots.txt"]
    assert conn.execute("SELECT policy FROM robots_cache").fetchone() == ("parsed",)


def retry_until(conn: sqlite3.Connection, server: FixtureServer) -> str | None:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (f"retry_until:http://localhost:{server.port}",)
    ).fetchone()
    return None if row is None else str(row[0])


@pytest.mark.anyio
async def test_retry_after_holds_origin_across_fetchers(
    conn: sqlite3.Connection, server: FixtureServer
) -> None:
    server.routes["/feed"] = (429, {"Retry-After": "120"}, b"slow down")
    async with make_fetcher(conn, server) as fetcher:
        with pytest.raises(FetchError):
            await fetcher.get(server.url("/feed"), kind="feed")
        with pytest.raises(FetchError) as held:
            await fetcher.get(server.url("/other"), kind="page")
    assert held.value.status is None
    assert held.value.retry_after_s is not None and 110 <= held.value.retry_after_s <= 120
    assert server.paths() == ["/feed"]
    stored = retry_until(conn, server)
    assert stored is not None and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", stored)
    async with make_fetcher(conn, server) as fetcher:
        with pytest.raises(FetchError) as again:
            await fetcher.get(server.url("/feed"), kind="feed")
    assert again.value.status is None
    assert server.paths() == ["/feed"]
    conn.execute("UPDATE meta SET value = '2000-01-01T00:00:00Z' WHERE key LIKE 'retry_until:%'")
    server.routes["/feed"] = (200, {}, b"ok")
    async with make_fetcher(conn, server) as fetcher:
        assert (await fetcher.get(server.url("/feed"), kind="feed")).status == 200
    assert server.paths() == ["/feed", "/feed"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("retry_after", "expected_s"), [(None, 3600.0), ("soon", 3600.0), ("999999", 86400.0)]
)
async def test_retry_until_defaults_to_an_hour_and_caps_at_a_day(
    conn: sqlite3.Connection, server: FixtureServer, retry_after: str | None, expected_s: float
) -> None:
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    server.routes["/feed"] = (503, headers, b"down")
    async with make_fetcher(conn, server) as fetcher:
        with pytest.raises(FetchError):
            await fetcher.get(server.url("/feed"), kind="feed")
        with pytest.raises(FetchError) as held:
            await fetcher.get(server.url("/feed"), kind="feed")
    assert held.value.retry_after_s is not None
    assert expected_s - 10 <= held.value.retry_after_s <= expected_s
    assert server.paths() == ["/feed"]


@pytest.mark.anyio
async def test_retry_until_is_memory_only_without_persist(
    conn: sqlite3.Connection, server: FixtureServer
) -> None:
    server.routes["/feed"] = (429, {"Retry-After": "120"}, b"slow down")
    async with make_fetcher(conn, server, persist=False) as fetcher:
        with pytest.raises(FetchError):
            await fetcher.get(server.url("/feed"), kind="feed")
        with pytest.raises(FetchError):
            await fetcher.get(server.url("/feed"), kind="feed")
    assert server.paths() == ["/feed"]
    assert retry_until(conn, server) is None


@pytest.mark.anyio
async def test_robots_429_disallows_all_and_holds_origin(
    conn: sqlite3.Connection, server: FixtureServer
) -> None:
    server.routes["/robots.txt"] = (429, {"Retry-After": "120"}, b"slow down")
    async with make_fetcher(conn, server, robots=True) as fetcher:
        with pytest.raises(BlockedError) as caught:
            await fetcher.get(server.url("/post"), kind="page")
    assert caught.value.reason == "robots"
    assert server.paths() == ["/robots.txt"]
    assert count(conn, "SELECT count(*) FROM robots_cache") == 0
    assert retry_until(conn, server) is not None
    async with make_fetcher(conn, server, robots=True) as fetcher:
        with pytest.raises(FetchError) as held:
            await fetcher.get(server.url("/post"), kind="page")
    assert held.value.status is None
    assert server.paths() == ["/robots.txt"]


@pytest.mark.anyio
async def test_https_to_http_redirect_is_refused(
    conn: sqlite3.Connection, server: FixtureServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fake only the TLS leg: https://localhost/secure is answered by the plain /bounce."""
    store_policy(conn, "https://localhost", RobotsPolicy("allow_all", None), datetime.now(UTC))
    server.routes["/bounce"] = (302, {"Location": server.url("/plain")}, b"")
    sent: list[str] = []
    async with make_fetcher(conn, server) as fetcher:
        real_exchange = vars(Fetcher)["_exchange"]

        async def exchange(url: str, headers: dict[str, str], bytes_max: int) -> Any:
            sent.append(url)
            return await real_exchange(fetcher, server.url("/bounce"), headers, bytes_max)

        monkeypatch.setattr(fetcher, "_exchange", exchange)
        with pytest.raises(BlockedError) as caught:
            await fetcher.get("https://localhost/secure", kind="page")
    assert caught.value.reason == "policy"
    assert sent == ["https://localhost/secure"]
    assert server.paths() == ["/bounce"]
