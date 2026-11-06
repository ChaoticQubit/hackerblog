"""The sites and poll_runs tables: config sync, resolved sources and poll health (7.1, 10)."""

import hashlib
import json
import sqlite3
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from hackerblog.core.config import SiteConfig

Method = Literal["feed", "sitemap", "linkdiff"]
Trigger = Literal["cli", "scheduler", "api"]
SELECT_SITES = (
    "SELECT id, slug, name, url, status, discovery_method, feed_url, sitemap_url, "
    "post_url_pattern, backfilled_at, last_polled_at, last_success_at, "
    "consecutive_failures, last_error FROM sites "
)


@dataclass(frozen=True)
class SiteRow:
    """One row of the sites table."""

    id: int
    slug: str
    name: str
    url: str
    status: str
    discovery_method: str | None
    feed_url: str | None
    sitemap_url: str | None
    post_url_pattern: str | None
    backfilled_at: str | None
    last_polled_at: str | None
    last_success_at: str | None
    consecutive_failures: int
    last_error: str | None


def utc_now() -> str:
    """Return the current UTC time as ISO-8601 text with a Z suffix, to the second."""
    text = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert len(text) == 20
    return text


@contextmanager
def transaction(conn: sqlite3.Connection) -> Generator[None]:
    """Run the block in one BEGIN IMMEDIATE transaction; roll back if it raises."""
    assert not conn.in_transaction, "nested transaction"
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def config_hash(site: SiteConfig) -> str:
    """Hash the keys that decide where a site's posts come from."""
    keys = [site.url, site.feed, site.sitemap, site.post_url_pattern]
    digest = hashlib.sha256(json.dumps(keys).encode()).hexdigest()
    assert len(digest) == 64
    return digest


def _row(values: tuple[Any, ...]) -> SiteRow:
    row = SiteRow(*values)
    assert row.id > 0
    assert row.consecutive_failures >= 0
    return row


def sync_sites(conn: sqlite3.Connection, configured: Sequence[SiteConfig], now: str) -> None:
    """Upsert every configured site by slug and mark the others `removed`.

    A change to url, feed, sitemap or post_url_pattern clears the resolved source, so
    the next poll resolves it again. A name found by discovery survives when the config
    gives none.
    """
    slugs = [site.slug for site in configured]
    assert all(slugs)
    assert len(set(slugs)) == len(slugs), "duplicate slugs pass config validation"
    with transaction(conn):
        for site in configured:
            _upsert(conn, site, now)
        conn.execute(
            "UPDATE sites SET status = 'removed', updated_at = ? WHERE status != 'removed' "
            "AND slug NOT IN (SELECT value FROM json_each(?))",
            (now, json.dumps(slugs)),
        )


def _upsert(conn: sqlite3.Connection, site: SiteConfig, now: str) -> None:
    assert site.slug is not None
    status = "disabled" if site.enabled is False else "active"
    conn.execute(
        """
        INSERT INTO sites (slug, name, url, status, post_url_pattern, config_hash,
                           created_at, updated_at)
        VALUES (:slug, COALESCE(:name, :slug), :url, :status, :pattern, :hash, :now, :now)
        ON CONFLICT (slug) DO UPDATE SET
            name = COALESCE(:name, sites.name),
            url = excluded.url,
            status = excluded.status,
            post_url_pattern = excluded.post_url_pattern,
            discovery_method = CASE WHEN sites.config_hash = excluded.config_hash
                THEN sites.discovery_method END,
            feed_url = CASE WHEN sites.config_hash = excluded.config_hash
                THEN sites.feed_url END,
            sitemap_url = CASE WHEN sites.config_hash = excluded.config_hash
                THEN sites.sitemap_url END,
            config_hash = excluded.config_hash,
            updated_at = excluded.updated_at
        """,
        {
            "slug": site.slug,
            "name": site.name,
            "url": site.url,
            "status": status,
            "pattern": site.post_url_pattern,
            "hash": config_hash(site),
            "now": now,
        },
    )


def get_site(conn: sqlite3.Connection, slug: str) -> SiteRow | None:
    """Return the site with this slug, in any status, or None."""
    found = conn.execute(SELECT_SITES + "WHERE slug = ?", (slug,)).fetchone()
    return None if found is None else _row(found)


def list_sites(conn: sqlite3.Connection, include_all: bool) -> list[SiteRow]:
    """Return sites ordered by slug; only `active` ones unless include_all."""
    where = "" if include_all else "WHERE status = 'active' "
    rows = conn.execute(SELECT_SITES + where + "ORDER BY slug").fetchall()
    return [_row(values) for values in rows]


def record_resolution(
    conn: sqlite3.Connection,
    site_id: int,
    method: Method,
    *,
    feed_url: str | None,
    sitemap_url: str | None,
    title: str | None,
) -> None:
    """Store how a site's posts are found; a page title replaces a slug-only name."""
    assert method != "feed" or feed_url is not None
    assert method != "sitemap" or sitemap_url is not None
    cursor = conn.execute(
        """
        UPDATE sites SET discovery_method = ?, feed_url = ?, sitemap_url = ?,
            name = CASE WHEN name = slug AND ? IS NOT NULL THEN ? ELSE name END
        WHERE id = ?
        """,
        (method, feed_url, sitemap_url, title, title, site_id),
    )
    assert cursor.rowcount == 1


def record_poll(conn: sqlite3.Connection, site_id: int, now: str, error: str | None) -> None:
    """Record one poll: success resets the failure streak, an error extends it.

    Retry-After holds (meta retry_until:<origin>) that have run out are pruned too.
    """
    if error is None:
        sql = (
            "UPDATE sites SET last_polled_at = ?, last_success_at = ?, "
            "consecutive_failures = 0, last_error = NULL WHERE id = ?"
        )
        cursor = conn.execute(sql, (now, now, site_id))
    else:
        assert error, "an error needs a message"
        sql = (
            "UPDATE sites SET last_polled_at = ?, last_error = ?, "
            "consecutive_failures = consecutive_failures + 1 WHERE id = ?"
        )
        cursor = conn.execute(sql, (now, error[:500], site_id))
    assert cursor.rowcount == 1
    conn.execute(
        "DELETE FROM meta WHERE key LIKE 'retry_until:%' AND value <= ?",
        (now,),
    )


def mark_backfilled(conn: sqlite3.Connection, site_id: int, now: str) -> None:
    """Record that the site's first full discovery finished."""
    cursor = conn.execute(
        "UPDATE sites SET backfilled_at = ? WHERE id = ? AND backfilled_at IS NULL",
        (now, site_id),
    )
    assert cursor.rowcount == 1, "a site is backfilled once"


def start_poll_run(conn: sqlite3.Connection, trigger: Trigger, now: str) -> int:
    """Insert a poll_runs row and return its id."""
    cursor = conn.execute(
        "INSERT INTO poll_runs (trigger, started_at) VALUES (?, ?)", (trigger, now)
    )
    run_id = cursor.lastrowid
    assert run_id is not None
    assert run_id > 0
    return run_id


def finish_poll_run(
    conn: sqlite3.Connection,
    run_id: int,
    now: str,
    *,
    sites_ok: int,
    sites_failed: int,
    new_posts: int,
) -> None:
    """Close a poll_runs row with its counts."""
    assert min(sites_ok, sites_failed, new_posts) >= 0
    cursor = conn.execute(
        "UPDATE poll_runs SET finished_at = ?, sites_ok = ?, sites_failed = ?, new_posts = ? "
        "WHERE id = ? AND finished_at IS NULL",
        (now, sites_ok, sites_failed, new_posts, run_id),
    )
    assert cursor.rowcount == 1
