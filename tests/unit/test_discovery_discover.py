import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from hackerblog.core.config import Defaults, SiteConfig
from hackerblog.core.discovery import discover
from hackerblog.core.discovery.discover import Limits
from hackerblog.core.discovery.model import Candidate
from hackerblog.core.store import sites
from hackerblog.core.store.migrate import connect, migrate

NOW = "2026-11-06T08:00:00Z"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "hb.db")
    migrate(connection)
    sites.sync_sites(connection, [SiteConfig(url="https://a.example/blog/")], NOW)
    yield connection
    connection.close()


def site_row(conn: sqlite3.Connection) -> sites.SiteRow:
    row = sites.get_site(conn, "a-example-blog")
    assert row is not None
    return row


def dated(number: int) -> Candidate:
    url = f"https://a.example/blog/post-number-{number}/"
    return Candidate(url=url, guid=url, published_at=f"2026-01-{number:02d}T00:00:00Z")


def statuses(conn: sqlite3.Connection) -> list[tuple[str, str, str | None]]:
    rows = conn.execute("SELECT url, status, skip_reason FROM posts ORDER BY url").fetchall()
    return [(url.rsplit("-", 1)[-1], status, reason) for url, status, reason in rows]


def test_limits_inherit_defaults_and_none_means_no_cap() -> None:
    defaults = Defaults()
    assert Limits.of(SiteConfig(url="https://a.example/"), defaults) == Limits(None, 20)
    capped = SiteConfig(url="https://a.example/", backfill_limit=0, max_new_per_poll=3)
    assert Limits.of(capped, defaults) == Limits(0, 3)
    assert (
        Limits.of(SiteConfig(url="https://a.example/"), Defaults(backfill_limit=5)).backfill_limit
        == 5
    )


def test_post_candidates_skip_unusable_locs_and_foreign_hosts(conn: sqlite3.Connection) -> None:
    pages: list[tuple[str, str | None]] = [
        ("ftp://a.example/blog/2026/old-ftp-post/", None),
        ("https://other.example/blog/2026/a-long-post-slug/", None),
        ("https://A.example/blog/2026/a-long-post-slug/?utm_source=x#top", "2026-01-01T00:00:00Z"),
        ("https://a.example/blog/tag/security/", None),
        ("https://a.example/blog/2026/" + "x" * 2100 + "/", None),
    ]
    found = discover.post_candidates(site_row(conn), pages)
    assert [item.url for item in found] == ["https://a.example/blog/2026/a-long-post-slug/"]
    assert found[0].published_at == "2026-01-01T00:00:00Z"


def test_backfill_queues_newest_up_to_the_limit_and_skips_robots(conn: sqlite3.Connection) -> None:
    row = site_row(conn)
    candidates = [dated(1), dated(3), dated(2), dated(4)]
    blocked = {dated(4).url}
    with sites.transaction(conn):
        counts = discover.store_backfill(
            conn, row.id, candidates, blocked, limits=Limits(2, 20), now=NOW
        )
    assert counts == (4, 2, 2)
    assert statuses(conn) == [
        ("1/", "skipped", "backfill_limit"),
        ("2/", "queued", None),
        ("3/", "queued", None),
        ("4/", "skipped", "robots"),
    ]


def test_uncapped_backfill_queues_everything(conn: sqlite3.Connection) -> None:
    row = site_row(conn)
    with sites.transaction(conn):
        counts = discover.store_backfill(
            conn, row.id, [dated(n) for n in range(1, 8)], set(), limits=Limits(None, 20), now=NOW
        )
    assert counts == (7, 7, 0)


def test_new_posts_over_the_cap_wait_for_the_next_poll(conn: sqlite3.Connection) -> None:
    row = site_row(conn)
    limits = Limits(None, 2)
    with sites.transaction(conn):
        first = discover.store_new(
            conn, row.id, [dated(1), dated(2), dated(3)], set(), limits=limits, now=NOW
        )
    assert first == (3, 2, 0)
    assert [status for _, status, _ in statuses(conn)] == ["queued", "queued", "discovered"]
    with sites.transaction(conn):
        second = discover.store_new(conn, row.id, [dated(1)], set(), limits=limits, now=NOW)
    assert second == (0, 1, 0)
    assert {status for _, status, _ in statuses(conn)} == {"queued"}


def test_feed_entries_on_other_hosts_are_dropped() -> None:
    urls = [
        "https://a.example/blog/one/",
        "https://www.a.example/blog/two/",
        "https://b.example/blog/three/",
        "https://evil-a.example/blog/four/",
    ]
    entries = [Candidate(url=url, guid=url) for url in urls]
    kept = discover.own_entries("https://www.a.example/blog/", entries)
    assert [item.url for item in kept] == urls[:2]


def test_feed_entries_on_subdomains_and_allowed_hosts_are_kept() -> None:
    urls = [
        "https://blog.example.com/one/",
        "https://deep.blog.example.com/two/",
        "https://news.example.org/three/",
        "https://notexample.com/four/",
        "https://example.com.evil.net/five/",
    ]
    entries = [Candidate(url=url, guid=url) for url in urls]
    subdomains = discover.own_entries("https://example.com/blog/", entries)
    assert [item.url for item in subdomains] == urls[:2]
    kept = discover.own_entries("https://example.com/blog/", entries, ("News.Example.org",))
    assert [item.url for item in kept] == urls[:3]


def test_allowed_hosts_must_be_host_names() -> None:
    site = SiteConfig(url="https://a.example/", allowed_hosts=["Blog.A.example"])
    assert site.allowed_hosts == ["blog.a.example"]
    for bad in ("https://b.example/", "b.example/path", "*.b.example", ""):
        with pytest.raises(ValueError, match="allowed_hosts"):
            SiteConfig(url="https://a.example/", allowed_hosts=[bad])
