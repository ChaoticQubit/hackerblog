"""Discovered posts: dedup on insert, queue or skip, and read queries (8.4, 8.5, 9, 10)."""

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from hackerblog.core.discovery.model import Candidate

Origin = Literal["backfill", "new", "manual"]
PROCESS_MAX_ATTEMPTS = 5
LIST_LIMIT_MAX = 500
SELECT_POSTS = (
    "SELECT p.id, s.slug, p.guid, p.url, p.canonical_url, p.title, p.author, p.published_at, "
    "p.discovered_at, p.origin, p.status, p.skip_reason, p.reading_status, p.last_error, "
    "p.processed_at, p.flags FROM posts p JOIN sites s ON s.id = p.site_id "
)


LIST_POSTS = """
    SELECT p.id, s.slug, p.guid, p.url, p.canonical_url, p.title, p.author, p.published_at,
           p.discovered_at, p.origin, p.status, p.skip_reason, p.reading_status, p.last_error,
           p.processed_at, p.flags
    FROM posts p JOIN sites s ON s.id = p.site_id
    WHERE (:sites IS NULL OR s.slug IN (SELECT value FROM json_each(:sites)))
      AND (:statuses IS NULL OR p.status IN (SELECT value FROM json_each(:statuses)))
      AND (:readings IS NULL OR p.reading_status IN (SELECT value FROM json_each(:readings)))
      AND (:since IS NULL OR p.published_at >= :since)
    ORDER BY p.published_at IS NULL, p.published_at DESC, p.id DESC
    LIMIT :limit
"""


@dataclass(frozen=True)
class PostRow:
    """One post with its site slug, without the stored body or digest."""

    id: int
    site: str
    guid: str
    url: str
    canonical_url: str
    title: str | None
    author: str | None
    published_at: str | None
    discovered_at: str
    origin: str
    status: str
    skip_reason: str | None
    reading_status: str
    last_error: str | None
    processed_at: str | None
    flags: tuple[str, ...]


@dataclass(frozen=True)
class PostQuery:
    """Filters for list_posts; None or an empty tuple means no filter on that field."""

    sites: tuple[str, ...] = ()
    statuses: tuple[str, ...] | None = None
    readings: tuple[str, ...] | None = None
    since: str | None = None
    limit: int = 20

    def __post_init__(self) -> None:
        assert 1 <= self.limit <= LIST_LIMIT_MAX, self.limit
        assert self.statuses is None or self.statuses
        assert self.readings is None or self.readings


def _row(values: tuple[Any, ...]) -> PostRow:
    row = PostRow(*values[:-1], flags=tuple(json.loads(values[-1])))
    assert row.id > 0
    assert row.canonical_url
    return row


def insert_post(
    conn: sqlite3.Connection, site_id: int, candidate: Candidate, origin: Origin, now: str
) -> int | None:
    """Insert a `discovered` post and return its id, or None if it is already known.

    Known means the same (site, guid) or the same canonical URL on any site.
    """
    assert site_id > 0
    found = conn.execute(
        """
        INSERT INTO posts (site_id, guid, url, canonical_url, title, author, published_at,
                           discovered_at, origin, status, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'discovered', ?)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        (
            site_id,
            candidate.guid,
            candidate.source_url or candidate.url,
            candidate.url,
            candidate.title,
            candidate.author,
            candidate.published_at,
            now,
            origin,
            now,
        ),
    ).fetchone()
    post_id: int | None = None if found is None else found[0]
    assert post_id is None or post_id > 0
    return post_id


def queue_post(conn: sqlite3.Connection, post_id: int, now: str) -> None:
    """Mark a discovered or skipped post `queued` and add its pending process_post job."""
    cursor = conn.execute(
        "UPDATE posts SET status = 'queued', skip_reason = NULL, updated_at = ? "
        "WHERE id = ? AND status IN ('discovered', 'skipped')",
        (now, post_id),
    )
    assert cursor.rowcount == 1, f"post {post_id} is not queueable"
    job = conn.execute(
        "INSERT INTO jobs (kind, post_id, stage, state, attempts, max_attempts, run_after, "
        "created_at, updated_at) VALUES ('process_post', ?, 'all', 'pending', 0, ?, ?, ?, ?)",
        (post_id, PROCESS_MAX_ATTEMPTS, now, now, now),
    )
    assert job.lastrowid is not None


def skip_post(conn: sqlite3.Connection, post_id: int, reason: str, now: str) -> None:
    """Mark a discovered post `skipped` with a reason such as robots or backfill_limit."""
    assert reason
    cursor = conn.execute(
        "UPDATE posts SET status = 'skipped', skip_reason = ?, updated_at = ? "
        "WHERE id = ? AND status = 'discovered'",
        (reason, now, post_id),
    )
    assert cursor.rowcount == 1, f"post {post_id} is not discovered"


def known_urls(conn: sqlite3.Connection, urls: Iterable[str]) -> set[str]:
    """Return the subset of canonical URLs already stored on any site."""
    rows = conn.execute(
        "SELECT canonical_url FROM posts WHERE canonical_url IN (SELECT value FROM json_each(?))",
        (json.dumps(list(urls)),),
    ).fetchall()
    return {row[0] for row in rows}


def site_urls(conn: sqlite3.Connection, site_id: int, limit: int) -> list[str]:
    """Return the canonical URLs of the site's newest stored posts, at most limit of them."""
    assert limit >= 1
    rows = conn.execute(
        "SELECT canonical_url FROM posts WHERE site_id = ? ORDER BY id DESC LIMIT ?",
        (site_id, limit),
    ).fetchall()
    assert len(rows) <= limit
    return [row[0] for row in rows]


def waiting_new(conn: sqlite3.Connection, site_id: int, limit: int) -> list[int]:
    """Return ids of new posts still waiting for a job, oldest discovery first."""
    assert limit >= 0
    rows = conn.execute(
        "SELECT id FROM posts WHERE site_id = ? AND origin = 'new' AND status = 'discovered' "
        "ORDER BY discovered_at, id LIMIT ?",
        (site_id, limit),
    ).fetchall()
    return [row[0] for row in rows]


def status_counts(conn: sqlite3.Connection, site_id: int) -> dict[str, int]:
    """Return the number of the site's posts per pipeline status."""
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM posts WHERE site_id = ? GROUP BY status ORDER BY status",
        (site_id,),
    ).fetchall()
    return dict(rows)


def get_post(conn: sqlite3.Connection, post_id: int) -> PostRow | None:
    """Return one post or None."""
    found = conn.execute(SELECT_POSTS + "WHERE p.id = ?", (post_id,)).fetchone()
    return None if found is None else _row(found)


def _json_or_none(values: tuple[str, ...] | None) -> str | None:
    return json.dumps(list(values)) if values else None


def list_posts(conn: sqlite3.Connection, query: PostQuery) -> list[PostRow]:
    """Return posts matching query, newest published first, undated last."""
    rows = conn.execute(
        LIST_POSTS,
        {
            "sites": _json_or_none(query.sites),
            "statuses": _json_or_none(query.statuses),
            "readings": _json_or_none(query.readings),
            "since": query.since,
            "limit": query.limit,
        },
    ).fetchall()
    found = [_row(values) for values in rows]
    assert len(found) <= query.limit
    return found
