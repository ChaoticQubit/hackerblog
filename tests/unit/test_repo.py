import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from tests.samples import RACE_POST_HTML, RACE_POST_URL

from hackerblog.core.extract import Extracted, extract
from hackerblog.core.store import repo
from hackerblog.core.store.migrate import connect, migrate

NOW = "2026-11-06T12:00:00Z"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = connect(tmp_path / "hb.db")
    migrate(db)
    yield db
    db.close()


@pytest.fixture(scope="module")
def race() -> Extracted:
    return extract(RACE_POST_HTML.read_bytes(), RACE_POST_URL)


def add_post(conn: sqlite3.Connection, n: int = 1) -> int:
    site_id = repo.ensure_adhoc_site(conn)
    url = f"https://x.example/p/{n}"
    post_id, created = repo.insert_manual_post(conn, site_id, url, url)
    assert created
    return post_id


def add_site(conn: sqlite3.Connection, slug: str) -> int:
    cur = conn.execute(
        "INSERT INTO sites (slug, name, url, config_hash, created_at, updated_at)"
        " VALUES (?, ?, ?, 'h', ?, ?)",
        (slug, slug, f"https://{slug}.example/", NOW, NOW),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def test_ensure_adhoc_site_is_idempotent(conn: sqlite3.Connection) -> None:
    first = repo.ensure_adhoc_site(conn)
    assert repo.ensure_adhoc_site(conn) == first
    row = conn.execute("SELECT slug, status FROM sites WHERE id = ?", (first,)).fetchone()
    assert row == ("adhoc", "disabled")


def test_insert_manual_post_dedups_on_canonical_url(conn: sqlite3.Connection) -> None:
    site_id = repo.ensure_adhoc_site(conn)
    canonical = "https://x.example/p/1"
    post_id, created = repo.insert_manual_post(conn, site_id, canonical + "?utm=1", canonical)
    assert created
    again, created_again = repo.insert_manual_post(conn, site_id, canonical + "#x", canonical)
    assert (again, created_again) == (post_id, False)
    post = repo.get_post(conn, post_id)
    assert post is not None
    assert (post.origin, post.status, post.site_slug) == ("manual", "queued", "adhoc")
    assert post.url == canonical + "?utm=1"
    assert post.flags == ()
    assert not post.truncated


def test_get_post_missing(conn: sqlite3.Connection) -> None:
    assert repo.get_post(conn, 42) is None


def test_set_post_status(conn: sqlite3.Connection) -> None:
    post_id = add_post(conn)
    repo.set_post_status(conn, post_id, "skipped", skip_reason="too_short")
    post = repo.get_post(conn, post_id)
    assert post is not None
    assert (post.status, post.skip_reason, post.last_error) == ("skipped", "too_short", None)
    repo.set_post_status(conn, post_id, "failed", error="HTTP 500")
    post = repo.get_post(conn, post_id)
    assert post is not None
    assert (post.status, post.skip_reason, post.last_error) == ("failed", None, "HTTP 500")


def test_set_post_status_rejects_unknown_status(conn: sqlite3.Connection) -> None:
    post_id = add_post(conn)
    with pytest.raises(AssertionError):
        repo.set_post_status(conn, post_id, "bogus")


def test_save_extracted_merges_flags_and_fills_metadata(
    conn: sqlite3.Connection, race: Extracted
) -> None:
    post_id = add_post(conn)
    conn.execute("UPDATE posts SET flags = '[\"paywalled\"]' WHERE id = ?", (post_id,))
    tagged = Extracted(
        race.title,
        race.author,
        race.published_at,
        race.body_md,
        race.word_count,
        race.content_hash,
        ("possible_injection",),
    )
    repo.save_extracted(conn, post_id, tagged, truncated=True)
    post = repo.get_post(conn, post_id)
    assert post is not None
    assert post.status == "extracted"
    assert post.body_md == race.body_md
    assert (post.word_count, post.content_hash) == (race.word_count, race.content_hash)
    assert post.truncated
    assert post.flags == ("paywalled", "possible_injection")
    assert (post.title, post.author, post.published_at) == (
        race.title,
        race.author,
        race.published_at,
    )


def test_save_extracted_keeps_feed_title(conn: sqlite3.Connection, race: Extracted) -> None:
    post_id = add_post(conn)
    conn.execute("UPDATE posts SET title = 'Feed title' WHERE id = ?", (post_id,))
    repo.save_extracted(conn, post_id, race, truncated=False)
    post = repo.get_post(conn, post_id)
    assert post is not None
    assert post.title == "Feed title"


def test_save_digest(conn: sqlite3.Connection) -> None:
    post_id = add_post(conn)
    repo.save_digest(conn, post_id, '{"title": "x"}')
    post = repo.get_post(conn, post_id)
    assert post is not None
    assert post.digest_json == '{"title": "x"}'


def test_mark_ready_indexes_and_find_by_hash(conn: sqlite3.Connection, race: Extracted) -> None:
    post_id = add_post(conn)
    repo.save_extracted(conn, post_id, race, truncated=False)
    assert repo.find_ready_by_hash(conn, race.content_hash) is None
    repo.mark_ready(
        conn, post_id, title="Races", summary="single packet", notes="n", body=race.body_md
    )
    repo.mark_ready(
        conn, post_id, title="Races", summary="single packet", notes="n", body=race.body_md
    )
    found = repo.find_ready_by_hash(conn, race.content_hash)
    assert found is not None
    assert found.id == post_id
    assert found.status == "ready"
    assert found.processed_at is not None and found.processed_at.endswith("Z")
    hits = conn.execute(
        "SELECT post_id FROM search_index WHERE search_index MATCH 'packet'"
    ).fetchall()
    assert hits == [(post_id,)]


def artifact(path: str, digit: str, size: int, template: str) -> dict[str, Any]:
    return {
        "path": path,
        "sha256": digit * 64,
        "size": size,
        "model": "m",
        "prompt_version": "p1",
        "template_version": template,
    }


def test_record_artifact_upserts(conn: sqlite3.Connection) -> None:
    post_id = add_post(conn)
    repo.record_artifact(conn, post_id, "lesson", **artifact("a/lesson.html", "0", 10, "t1"))
    repo.record_artifact(conn, post_id, "lesson", **artifact("a/lesson.html", "1", 20, "t2"))
    repo.record_artifact(conn, post_id, "notes", **artifact("a/notes.md", "2", 5, "t2"))
    found = repo.artifacts_for(conn, post_id)
    assert [(a.kind, a.sha256[0], a.bytes, a.template_version) for a in found] == [
        ("lesson", "1", 20, "t2"),
        ("notes", "2", 5, "t2"),
    ]


def test_ready_posts_filters_by_site(conn: sqlite3.Connection) -> None:
    adhoc_post = add_post(conn, 1)
    other_site = add_site(conn, "blog")
    other_post, _ = repo.insert_manual_post(conn, other_site, "https://b/1", "https://b/1")
    add_post(conn, 3)
    for post_id in (adhoc_post, other_post):
        repo.mark_ready(conn, post_id, title="t", summary="s", notes="n", body="b")
    assert {p.id for p in repo.ready_posts(conn)} == {adhoc_post, other_post}
    assert [p.id for p in repo.ready_posts(conn, ("blog",))] == [other_post]
    assert repo.ready_posts(conn, ("nope",)) == []


def test_flags_stored_as_json_list(conn: sqlite3.Connection, race: Extracted) -> None:
    post_id = add_post(conn)
    repo.save_extracted(conn, post_id, race, truncated=False)
    raw = conn.execute("SELECT flags FROM posts WHERE id = ?", (post_id,)).fetchone()[0]
    assert json.loads(raw) == []
