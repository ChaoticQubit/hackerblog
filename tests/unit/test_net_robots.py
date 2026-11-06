from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hackerblog.core.net.robots import RobotsPolicy, load_policy, origin_of, store_policy
from hackerblog.core.store.migrate import connect, migrate

UA = "HackerBlog/0.1.0 (+https://github.com/hackerblog/hackerblog)"
BODY = (
    "User-agent: *\nDisallow: /private\nCrawl-delay: 3\n"
    "User-agent: HackerBlog\nDisallow: /nobots\nRequest-rate: 1/10s\n"
    "Sitemap: https://blog.example/sitemap.xml\n"
)


def test_parsed_policy_uses_our_group() -> None:
    policy = RobotsPolicy("parsed", BODY)
    assert not policy.allows("https://blog.example/nobots/x", UA)
    assert policy.allows("https://blog.example/private", UA)
    assert policy.delay_s(UA) == 10.0
    assert policy.sitemaps() == ["https://blog.example/sitemap.xml"]


def test_crawl_delay_for_other_agents() -> None:
    policy = RobotsPolicy("parsed", BODY)
    assert not policy.allows("https://blog.example/private", "OtherBot/1.0")
    assert policy.delay_s("OtherBot/1.0") == 3.0


def test_allow_all_and_disallow_all() -> None:
    assert RobotsPolicy("allow_all", None).allows("https://blog.example/x", UA)
    assert not RobotsPolicy("disallow_all", None).allows("https://blog.example/x", UA)
    assert RobotsPolicy("allow_all", None).delay_s(UA) == 0.0
    assert RobotsPolicy("disallow_all", None).sitemaps() == []


def test_origin_of() -> None:
    assert origin_of("https://Blog.Example/a?b") == "https://blog.example"
    assert origin_of("http://blog.example:8080/a") == "http://blog.example:8080"
    assert origin_of("http://[::1]:8080/") == "http://[::1]:8080"


def test_cache_round_trip_and_expiry(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite")
    migrate(conn)
    now = datetime(2026, 11, 6, 12, tzinfo=UTC)
    store_policy(conn, "https://blog.example", RobotsPolicy("parsed", BODY), now)
    loaded = load_policy(conn, "https://blog.example", now + timedelta(hours=23))
    assert loaded == RobotsPolicy("parsed", BODY)
    assert load_policy(conn, "https://blog.example", now + timedelta(hours=25)) is None
    store_policy(conn, "https://blog.example", RobotsPolicy("allow_all", None), now)
    assert load_policy(conn, "https://blog.example", now) == RobotsPolicy("allow_all", None)
    assert load_policy(conn, "https://other.example", now) is None
    stored = conn.execute("SELECT fetched_at FROM robots_cache").fetchone()[0]
    assert stored == "2026-11-06T12:00:00Z"


def test_disallow_all_is_never_cached(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite")
    migrate(conn)
    now = datetime(2026, 11, 6, 12, tzinfo=UTC)
    with pytest.raises(AssertionError):
        store_policy(conn, "https://blog.example", RobotsPolicy("disallow_all", None), now)
