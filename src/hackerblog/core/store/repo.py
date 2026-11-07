"""Processing repository: posts, extraction and digest results, artifacts, search index."""

import json
import sqlite3
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import Any

from hackerblog.core.extract import Extracted

POST_STATUSES = frozenset(
    {
        "discovered",
        "queued",
        "fetching",
        "extracted",
        "generating",
        "rendering",
        "ready",
        "skipped",
        "failed",
        "dead",
    }
)
ARTIFACT_KINDS = frozenset({"notes", "lesson", "digest"})
ADHOC_SLUG = "adhoc"
POST_SELECT = "SELECT p.*, s.slug FROM posts p JOIN sites s ON s.id = p.site_id"


def iso(moment: datetime) -> str:
    """Format an aware datetime as UTC ISO-8601 with seconds and a literal Z."""
    assert moment.tzinfo is not None, "naive datetime"
    text = moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert len(text) == 20
    return text


def utc_now() -> str:
    """Return the current time as UTC ISO-8601 with a Z."""
    return iso(datetime.now(UTC))


@contextmanager
def transaction(conn: sqlite3.Connection) -> Generator[sqlite3.Connection]:
    """Run the block in one BEGIN IMMEDIATE transaction; commit on success, roll back on error."""
    assert not conn.in_transaction, "nested transaction"
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    assert not conn.in_transaction


@dataclass(frozen=True)
class PostRow:
    """One posts row, in table column order, plus its site slug. flags is a tuple."""

    id: int
    site_id: int
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
    body_md: str | None
    word_count: int | None
    content_hash: str | None
    truncated: bool
    flags: tuple[str, ...]
    digest_json: str | None
    last_error: str | None
    processed_at: str | None
    updated_at: str
    site_slug: str


@dataclass(frozen=True)
class ArtifactRow:
    """One artifacts row: a file written for a post, with its hash, size and versions."""

    id: int
    post_id: int
    kind: str
    path: str
    sha256: str
    bytes: int
    model: str | None
    prompt_version: str | None
    template_version: str
    created_at: str


def _post(row: tuple[Any, ...]) -> PostRow:
    values = list(row)
    assert len(values) == len(fields(PostRow)), "posts columns changed"
    values[16] = bool(values[16])
    values[17] = tuple(json.loads(values[17]))
    post = PostRow(*values)
    assert post.status in POST_STATUSES
    return post


def get_post(conn: sqlite3.Connection, post_id: int) -> PostRow | None:
    """Return the post with post_id, or None."""
    row = conn.execute(POST_SELECT + " WHERE p.id = ?", (post_id,)).fetchone()
    return None if row is None else _post(row)


def set_post_status(
    conn: sqlite3.Connection,
    post_id: int,
    status: str,
    skip_reason: str | None = None,
    error: str | None = None,
) -> None:
    """Set a post's pipeline status, replacing its skip_reason and last_error.

    One statement, so it is safe inside a caller's transaction.
    """
    assert status in POST_STATUSES, f"unknown post status {status!r}"
    cur = conn.execute(
        "UPDATE posts SET status = ?, skip_reason = ?, last_error = ?, updated_at = ? WHERE id = ?",
        (status, skip_reason, error, utc_now(), post_id),
    )
    assert cur.rowcount == 1, f"no post {post_id}"


def save_extracted(
    conn: sqlite3.Connection, post_id: int, extracted: Extracted, truncated: bool
) -> None:
    """Store extracted text, size, hash and truncation, merge flags and set status extracted.

    Title, author and published_at from the page fill only fields the feed left empty.
    """
    assert len(extracted.content_hash) == 64
    with transaction(conn):
        row = conn.execute("SELECT flags FROM posts WHERE id = ?", (post_id,)).fetchone()
        assert row is not None, f"no post {post_id}"
        merged = sorted(set(json.loads(row[0])) | set(extracted.flags))
        conn.execute(
            "UPDATE posts SET status = 'extracted', body_md = ?, word_count = ?,"
            " content_hash = ?, truncated = ?, flags = ?, title = COALESCE(title, ?),"
            " author = COALESCE(author, ?), published_at = COALESCE(published_at, ?),"
            " updated_at = ? WHERE id = ?",
            (
                extracted.body_md,
                extracted.word_count,
                extracted.content_hash,
                int(truncated),
                json.dumps(merged),
                extracted.title,
                extracted.author,
                extracted.published_at,
                utc_now(),
                post_id,
            ),
        )


def save_digest(conn: sqlite3.Connection, post_id: int, digest_json: str) -> None:
    """Store the validated PostDigest JSON for a post."""
    assert digest_json.startswith("{")
    cur = conn.execute(
        "UPDATE posts SET digest_json = ?, updated_at = ? WHERE id = ?",
        (digest_json, utc_now(), post_id),
    )
    assert cur.rowcount == 1, f"no post {post_id}"


def find_ready_by_hash(conn: sqlite3.Connection, content_hash: str) -> PostRow | None:
    """Return the oldest ready post whose extracted text has content_hash, or None."""
    assert len(content_hash) == 64
    row = conn.execute(
        POST_SELECT + " WHERE p.content_hash = ? AND p.status = 'ready' ORDER BY p.id LIMIT 1",
        (content_hash,),
    ).fetchone()
    found = None if row is None else _post(row)
    assert found is None or found.content_hash == content_hash
    return found


def record_artifact(
    conn: sqlite3.Connection,
    post_id: int,
    kind: str,
    *,
    path: str,
    sha256: str,
    size: int,
    model: str | None,
    prompt_version: str | None,
    template_version: str,
) -> None:
    """Insert or replace the artifact of kind for post_id."""
    assert kind in ARTIFACT_KINDS, f"unknown artifact kind {kind!r}"
    assert len(sha256) == 64 and size >= 0
    conn.execute(
        "INSERT INTO artifacts (post_id, kind, path, sha256, bytes, model, prompt_version,"
        " template_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (post_id, kind) DO UPDATE SET path = excluded.path,"
        " sha256 = excluded.sha256, bytes = excluded.bytes, model = excluded.model,"
        " prompt_version = excluded.prompt_version,"
        " template_version = excluded.template_version, created_at = excluded.created_at",
        (post_id, kind, path, sha256, size, model, prompt_version, template_version, utc_now()),
    )


def artifacts_for(conn: sqlite3.Connection, post_id: int) -> list[ArtifactRow]:
    """Return the artifacts of post_id ordered by kind."""
    rows = conn.execute(
        "SELECT id, post_id, kind, path, sha256, bytes, model, prompt_version,"
        " template_version, created_at FROM artifacts WHERE post_id = ? ORDER BY kind",
        (post_id,),
    ).fetchall()
    found = [ArtifactRow(*row) for row in rows]
    assert all(a.post_id == post_id for a in found)
    return found


def mark_ready(
    conn: sqlite3.Connection, post_id: int, *, title: str, summary: str, notes: str, body: str
) -> None:
    """Set a post ready with processed_at now and replace its search_index row, atomically."""
    assert title, "ready post needs a title"
    now = utc_now()
    with transaction(conn):
        cur = conn.execute(
            "UPDATE posts SET status = 'ready', skip_reason = NULL, last_error = NULL,"
            " processed_at = ?, updated_at = ? WHERE id = ?",
            (now, now, post_id),
        )
        assert cur.rowcount == 1, f"no post {post_id}"
        conn.execute("DELETE FROM search_index WHERE post_id = ?", (post_id,))
        conn.execute(
            "INSERT INTO search_index (title, summary, notes, body, post_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (title, summary, notes, body, post_id),
        )


def ensure_adhoc_site(conn: sqlite3.Connection) -> int:
    """Return the id of the disabled 'adhoc' site for manual URLs, creating it if missing."""
    now = utc_now()
    conn.execute(
        "INSERT INTO sites (slug, name, url, status, config_hash, created_at, updated_at)"
        " VALUES (?, 'Ad hoc', '', 'disabled', '', ?, ?) ON CONFLICT (slug) DO NOTHING",
        (ADHOC_SLUG, now, now),
    )
    row = conn.execute("SELECT id FROM sites WHERE slug = ?", (ADHOC_SLUG,)).fetchone()
    assert row is not None
    return int(row[0])


def insert_manual_post(
    conn: sqlite3.Connection, site_id: int, url: str, canonical_url: str
) -> tuple[int, bool]:
    """Insert a queued manual post, or find the one with canonical_url.

    Returns (post_id, created). The caller enqueues its process_post job.
    """
    assert url and canonical_url
    now = utc_now()
    with transaction(conn):
        row = conn.execute(
            "SELECT id FROM posts WHERE canonical_url = ?", (canonical_url,)
        ).fetchone()
        if row is not None:
            return int(row[0]), False
        cur = conn.execute(
            "INSERT INTO posts (site_id, guid, url, canonical_url, discovered_at, origin,"
            " status, updated_at) VALUES (?, ?, ?, ?, ?, 'manual', 'queued', ?)",
            (site_id, canonical_url, url, canonical_url, now, now),
        )
    assert cur.lastrowid is not None
    return cur.lastrowid, True


def ready_posts(conn: sqlite3.Connection, sites: tuple[str, ...] = ()) -> list[PostRow]:
    """Return ready posts, newest published first, limited to the given site slugs if any."""
    wanted = json.dumps(list(sites))
    rows = conn.execute(
        "SELECT p.*, s.slug FROM posts p JOIN sites s ON s.id = p.site_id"
        " WHERE p.status = 'ready' AND (? = '[]' OR s.slug IN (SELECT value FROM json_each(?)))"
        " ORDER BY COALESCE(p.published_at, p.discovered_at) DESC, p.id DESC",
        (wanted, wanted),
    ).fetchall()
    found = [_post(row) for row in rows]
    assert all(post.status == "ready" for post in found)
    return found


def set_flags(conn: sqlite3.Connection, post_id: int, flags: Sequence[str]) -> None:
    """Replace a post's reader flags with flags, sorted and without duplicates."""
    assert all(flags), "empty flag"
    cur = conn.execute(
        "UPDATE posts SET flags = ?, updated_at = ? WHERE id = ?",
        (json.dumps(sorted(set(flags))), utc_now(), post_id),
    )
    assert cur.rowcount == 1, f"no post {post_id}"
