import json
import sqlite3
from pathlib import Path

import pytest
from tests.samples import digest_data, lesson_data

from hackerblog.core.store.migrate import connect, migrate
from hackerblog.core.store.search import fts_query, search

NOW = "2026-11-06T10:00:00Z"


def make_db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "hb.db")
    migrate(conn)
    return conn


def add_site(conn: sqlite3.Connection, slug: str) -> int:
    cursor = conn.execute(
        "INSERT INTO sites (slug, name, url, config_hash, created_at, updated_at) "
        "VALUES (?, ?, ?, 'h', ?, ?)",
        (slug, slug.title(), f"https://{slug}.example.com/", NOW, NOW),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def add_post(
    conn: sqlite3.Connection,
    site_id: int,
    title: str,
    *,
    body: str = "Body text about nothing in particular.",
    status: str = "ready",
    out_dir: Path | None = None,
    published_at: str = "2026-11-01T09:00:00Z",
) -> int:
    digest = digest_data()
    digest["title"] = title
    cursor = conn.execute(
        "INSERT INTO posts (site_id, guid, url, canonical_url, title, published_at, "
        "discovered_at, origin, status, body_md, digest_json, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?)",
        (
            site_id,
            title,
            f"https://example.com/{site_id}/{abs(hash(title))}",
            f"https://example.com/{site_id}/{abs(hash(title))}",
            title,
            published_at,
            NOW,
            status,
            body,
            json.dumps(digest),
            NOW,
        ),
    )
    post_id = cursor.lastrowid
    assert post_id is not None
    if status == "ready":
        conn.execute(
            "INSERT INTO search_index (title, summary, notes, body, post_id) VALUES (?,?,?,?,?)",
            (title, f"Summary of {title}", f"Notes on {title}", body, post_id),
        )
    if out_dir is not None:
        add_artifacts(conn, post_id, out_dir / str(post_id))
    return post_id


def add_artifacts(conn: sqlite3.Connection, post_id: int, folder: Path) -> None:
    folder.mkdir(parents=True)
    files = {"lesson": "lesson.html", "notes": "notes.md", "digest": "post.json"}
    document = {"schema_version": 1, "post": {}, "digest": digest_data(), "lesson": lesson_data()}
    (folder / "lesson.html").write_text("<html></html>", encoding="utf-8")
    (folder / "notes.md").write_text("# notes", encoding="utf-8")
    (folder / "post.json").write_text(json.dumps(document), encoding="utf-8")
    for kind, name in files.items():
        conn.execute(
            "INSERT INTO artifacts (post_id, kind, path, sha256, bytes, template_version, "
            "created_at) VALUES (?, ?, ?, 'x', 1, '1', ?)",
            (post_id, kind, str(folder / name), NOW),
        )


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    conn = make_db(tmp_path)
    alpha = add_site(conn, "alpha")
    beta = add_site(conn, "beta")
    add_post(conn, alpha, "Limit overrun races", body="Race windows and the single packet attack.")
    add_post(conn, beta, "Heap grooming basics", body="Allocator tricks, no races here at all.")
    add_post(conn, beta, 'Quote "NEAR" stars*', body="title: column filter text NEAR(a b)")
    return conn


def test_fts_query_quotes_every_token() -> None:
    assert fts_query("race window") == '"race" "window"'
    assert fts_query('say "hi"') == '"say" """hi"""'
    assert fts_query("  ") == ""


def test_search_ranks_title_matches_first(conn: sqlite3.Connection) -> None:
    hits = search(conn, "races", limit=10)
    assert hits[0].title == "Limit overrun races"
    assert len(hits) == 2
    assert hits[0].site == "alpha"
    assert "[" in hits[0].snippet
    assert "]" in hits[0].snippet


def test_search_filters_by_site(conn: sqlite3.Connection) -> None:
    hits = search(conn, "races", site="beta", limit=10)
    assert [hit.title for hit in hits] == ["Heap grooming basics"]


def test_search_respects_limit(conn: sqlite3.Connection) -> None:
    assert len(search(conn, "races", limit=1)) == 1


@pytest.mark.parametrize(
    "text",
    [
        '"',
        '""',
        "NEAR(a b)",
        "a OR b",
        "race*",
        "title:race",
        "-race",
        "^race",
        "(",
        "a AND",
        "{title body}: x",
        "'; DROP TABLE posts; --",
    ],
)
def test_search_never_raises_on_fts_syntax(conn: sqlite3.Connection, text: str) -> None:
    search(conn, text, limit=10)
    assert conn.execute("SELECT count(*) FROM posts").fetchone()[0] == 3


def test_column_filter_is_literal_text(conn: sqlite3.Connection) -> None:
    hits = search(conn, "title:race", limit=10)
    assert all(hit.title != "Limit overrun races" for hit in hits)


def test_star_is_not_a_prefix_query(conn: sqlite3.Connection) -> None:
    assert search(conn, "rac*", limit=10) == []


def test_blank_query_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="empty"):
        search(conn, "   ", limit=10)


@pytest.mark.parametrize("status", ["skipped", "failed", "generating"])
def test_search_hides_stale_rows_of_posts_no_longer_ready(
    conn: sqlite3.Connection, status: str
) -> None:
    conn.execute("UPDATE posts SET status = ? WHERE title = 'Limit overrun races'", (status,))
    assert [hit.title for hit in search(conn, "races", limit=10)] == ["Heap grooming basics"]
