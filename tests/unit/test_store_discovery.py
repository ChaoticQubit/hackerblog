import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from hackerblog.core.config import SiteConfig
from hackerblog.core.discovery.model import Candidate
from hackerblog.core.store import posts, sites
from hackerblog.core.store.migrate import connect, migrate

NOW = "2026-11-06T08:00:00Z"
LATER = "2026-11-07T08:00:00Z"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "hb.db")
    migrate(connection)
    yield connection
    connection.close()


def site(url: str, **extra: object) -> SiteConfig:
    return SiteConfig.model_validate({"url": url, **extra})


def only(conn: sqlite3.Connection, slug: str) -> sites.SiteRow:
    row = sites.get_site(conn, slug)
    assert row is not None
    return row


def test_sync_inserts_updates_and_marks_removed(conn: sqlite3.Connection) -> None:
    sites.sync_sites(conn, [site("https://a.example/blog/"), site("https://b.example/")], NOW)
    first = only(conn, "a-example-blog")
    assert first.name == "a-example-blog"
    assert first.status == "active"
    sites.sync_sites(conn, [site("https://a.example/blog/", name="A", enabled=False)], LATER)
    assert only(conn, "a-example-blog").name == "A"
    assert only(conn, "a-example-blog").status == "disabled"
    assert only(conn, "b-example").status == "removed"
    assert [row.slug for row in sites.list_sites(conn, include_all=False)] == []
    assert len(sites.list_sites(conn, include_all=True)) == 2


def test_resolution_kept_until_source_config_changes(conn: sqlite3.Connection) -> None:
    sites.sync_sites(conn, [site("https://a.example/")], NOW)
    row = only(conn, "a-example")
    sites.record_resolution(
        conn, row.id, "feed", feed_url="https://a.example/feed", sitemap_url=None, title="Blog A"
    )
    sites.sync_sites(conn, [site("https://a.example/", tags=["x"])], LATER)
    kept = only(conn, "a-example")
    assert kept.discovery_method == "feed"
    assert kept.name == "Blog A"
    sites.sync_sites(conn, [site("https://a.example/", feed="https://a.example/rss")], LATER)
    reset = only(conn, "a-example")
    assert reset.discovery_method is None
    assert reset.feed_url is None


def test_poll_health_counts_failures_and_resets(conn: sqlite3.Connection) -> None:
    sites.sync_sites(conn, [site("https://a.example/")], NOW)
    site_id = only(conn, "a-example").id
    sites.record_poll(conn, site_id, NOW, "timeout")
    sites.record_poll(conn, site_id, NOW, "timeout")
    failing = only(conn, "a-example")
    assert failing.consecutive_failures == 2
    assert failing.last_error == "timeout"
    assert failing.last_success_at is None
    sites.record_poll(conn, site_id, LATER, None)
    healthy = only(conn, "a-example")
    assert healthy.consecutive_failures == 0
    assert healthy.last_success_at == LATER


def candidate(url: str, guid: str | None = None, published: str | None = None) -> Candidate:
    return Candidate(url=url, guid=guid or url, title="T", published_at=published)


def test_insert_dedups_by_guid_and_canonical_url(conn: sqlite3.Connection) -> None:
    sites.sync_sites(conn, [site("https://a.example/"), site("https://b.example/")], NOW)
    a_id = only(conn, "a-example").id
    b_id = only(conn, "b-example").id
    first = posts.insert_post(conn, a_id, candidate("https://a.example/p/1"), "backfill", NOW)
    assert first is not None
    same_guid = candidate("https://a.example/p/1b", guid="https://a.example/p/1")
    assert posts.insert_post(conn, a_id, same_guid, "new", NOW) is None
    assert posts.insert_post(conn, b_id, candidate("https://a.example/p/1"), "new", NOW) is None
    assert posts.known_urls(conn, ["https://a.example/p/1", "https://a.example/p/2"]) == {
        "https://a.example/p/1"
    }


def test_queue_creates_one_pending_job_and_skip_sets_reason(conn: sqlite3.Connection) -> None:
    sites.sync_sites(conn, [site("https://a.example/")], NOW)
    site_id = only(conn, "a-example").id
    queued = posts.insert_post(conn, site_id, candidate("https://a.example/p/1"), "new", NOW)
    skipped = posts.insert_post(conn, site_id, candidate("https://a.example/p/2"), "backfill", NOW)
    assert queued is not None
    assert skipped is not None
    posts.queue_post(conn, queued, NOW)
    posts.skip_post(conn, skipped, "robots", NOW)
    jobs = conn.execute("SELECT post_id, kind, state, stage FROM jobs").fetchall()
    assert jobs == [(queued, "process_post", "pending", "all")]
    detail = posts.get_post(conn, skipped)
    assert detail is not None
    assert detail.status == "skipped"
    assert detail.skip_reason == "robots"
    assert posts.status_counts(conn, site_id) == {"queued": 1, "skipped": 1}


def test_waiting_new_posts_are_oldest_first(conn: sqlite3.Connection) -> None:
    sites.sync_sites(conn, [site("https://a.example/")], NOW)
    site_id = only(conn, "a-example").id
    for number in range(3):
        url = f"https://a.example/p/{number}"
        posts.insert_post(conn, site_id, candidate(url), "new", f"2026-11-0{number + 1}T00:00:00Z")
    waiting = posts.waiting_new(conn, site_id, limit=2)
    assert len(waiting) == 2
    assert waiting == sorted(waiting)


def test_list_posts_filters_by_site_status_and_since(conn: sqlite3.Connection) -> None:
    sites.sync_sites(conn, [site("https://a.example/"), site("https://b.example/")], NOW)
    a_id = only(conn, "a-example").id
    b_id = only(conn, "b-example").id
    old = candidate("https://a.example/old", published="2025-01-01T00:00:00Z")
    new = candidate("https://a.example/new", published="2026-11-01T00:00:00Z")
    other = candidate("https://b.example/x", published="2026-11-02T00:00:00Z")
    for site_id, item in ((a_id, old), (a_id, new), (b_id, other)):
        posts.insert_post(conn, site_id, item, "backfill", NOW)
    query = posts.PostQuery(sites=("a-example",), statuses=None, since="2026-01-01", limit=20)
    found = posts.list_posts(conn, query)
    assert [row.url for row in found] == ["https://a.example/new"]
    everything = posts.list_posts(conn, posts.PostQuery(limit=20))
    assert [row.url for row in everything] == [
        "https://b.example/x",
        "https://a.example/new",
        "https://a.example/old",
    ]
    none = posts.list_posts(conn, posts.PostQuery(statuses=("ready",), limit=20))
    assert none == []


def test_poll_run_rows(conn: sqlite3.Connection) -> None:
    run_id = sites.start_poll_run(conn, "cli", NOW)
    sites.finish_poll_run(conn, run_id, LATER, sites_ok=2, sites_failed=1, new_posts=4)
    row = conn.execute(
        "SELECT finished_at, sites_ok, sites_failed, new_posts FROM poll_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    assert row == (LATER, 2, 1, 4)


def test_insert_keeps_the_source_url_beside_the_canonical_one(conn: sqlite3.Connection) -> None:
    sites.sync_sites(conn, [site("https://a.example/")], NOW)
    raw = "https://A.example/p/1?utm_source=feed"
    item = Candidate(url="https://a.example/p/1", guid="g1", source_url=raw)
    post_id = posts.insert_post(conn, only(conn, "a-example").id, item, "new", NOW)
    assert post_id is not None
    found = posts.get_post(conn, post_id)
    assert found is not None
    assert found.url == raw
    assert found.canonical_url == "https://a.example/p/1"


def test_record_poll_prunes_expired_retry_holds(conn: sqlite3.Connection) -> None:
    sites.sync_sites(conn, [site("https://a.example/")], NOW)
    conn.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        [
            ("retry_until:https://old.example", "2026-11-06T07:59:59Z"),
            ("retry_until:https://due.example", NOW),
            ("retry_until:https://later.example", LATER),
            ("last_digest_at", "2020-01-01T00:00:00Z"),
        ],
    )
    sites.record_poll(conn, only(conn, "a-example").id, NOW, None)
    keys = [row[0] for row in conn.execute("SELECT key FROM meta ORDER BY key")]
    assert keys == ["last_digest_at", "retry_until:https://later.example"]
