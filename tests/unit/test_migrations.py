import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hackerblog.core import jobs
from hackerblog.core.store import repo
from hackerblog.core.store.migrate import MIGRATIONS_DIR, connect, migrate

EXPECTED_TABLES = {
    "sites",
    "http_cache",
    "robots_cache",
    "posts",
    "jobs",
    "artifacts",
    "llm_cache",
    "llm_usage",
    "poll_runs",
    "events",
    "notifications",
    "quiz_attempts",
    "meta",
    "search_index",
}


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def test_fresh_database_gets_full_schema(tmp_path: Path) -> None:
    conn = connect(tmp_path / "hb.db")
    assert migrate(conn) == 2
    assert table_names(conn) >= EXPECTED_TABLES
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    conn = connect(tmp_path / "hb.db")
    migrate(conn)
    assert migrate(conn) == 0
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_fts5_search_works(tmp_path: Path) -> None:
    conn = connect(tmp_path / "hb.db")
    migrate(conn)
    conn.execute(
        "INSERT INTO search_index (title, summary, notes, body, post_id) VALUES (?, ?, ?, ?, ?)",
        ("Racing requests", "single packet attacks", "", "", 7),
    )
    hits = conn.execute(
        "SELECT post_id FROM search_index WHERE search_index MATCH ?", ('"packet"',)
    ).fetchall()
    assert hits == [(7,)]


def test_one_open_job_per_post(tmp_path: Path) -> None:
    conn = connect(tmp_path / "hb.db")
    migrate(conn)
    now = "2026-11-06T00:00:00Z"
    conn.execute(
        "INSERT INTO sites (slug, name, url, config_hash, created_at, updated_at)"
        " VALUES ('s', 'S', 'https://example.org/', 'h', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO posts (site_id, guid, url, canonical_url, discovered_at, origin, status,"
        " updated_at) VALUES (1, 'g', 'https://example.org/p', 'https://example.org/p', ?,"
        " 'new', 'queued', ?)",
        (now, now),
    )
    insert_job = (
        "INSERT INTO jobs (kind, post_id, state, max_attempts, run_after, created_at, updated_at)"
        " VALUES ('process_post', 1, 'pending', 5, ?, ?, ?)"
    )
    conn.execute(insert_job, (now, now, now))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert_job, (now, now, now))


def test_failed_migration_rolls_back(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_good.sql").write_text("CREATE TABLE a (id INTEGER);", encoding="utf-8")
    (migrations / "0002_bad.sql").write_text(
        "CREATE TABLE b (id INTEGER);\nCREATE TABLE broken (;", encoding="utf-8"
    )
    conn = connect(tmp_path / "hb.db")
    with pytest.raises(sqlite3.OperationalError):
        migrate(conn, migrations)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert "a" in table_names(conn)
    assert "b" not in table_names(conn)


def test_migration_numbers_must_be_contiguous(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_a.sql").write_text("CREATE TABLE a (id INTEGER);", encoding="utf-8")
    (migrations / "0003_c.sql").write_text("CREATE TABLE c (id INTEGER);", encoding="utf-8")
    conn = connect(tmp_path / "hb.db")
    with pytest.raises(AssertionError):
        migrate(conn, migrations)


def test_a_job_claimed_before_runs_existed_can_be_reclaimed_and_settled(tmp_path: Path) -> None:
    v1 = tmp_path / "v1"
    v1.mkdir()
    shutil.copy(MIGRATIONS_DIR / "0001_initial.sql", v1)
    conn = connect(tmp_path / "upgraded.db")
    assert migrate(conn, v1) == 1
    site_id = repo.ensure_adhoc_site(conn)
    url = "https://alpha.example/post"
    post_id, _ = repo.insert_manual_post(conn, site_id, url, url)
    conn.execute(
        "INSERT INTO jobs (kind, post_id, state, attempts, max_attempts, run_after,"
        " lease_until, created_at, updated_at) VALUES ('process_post', ?, 'running', 2, 5,"
        " '2026-01-01T00:00:00Z', '2026-01-01T00:10:00Z', '2026-01-01T00:00:00Z',"
        " '2026-01-01T00:00:00Z')",
        (post_id,),
    )
    conn.commit()
    assert migrate(conn) == 1
    job = jobs.claim(conn, datetime.now(UTC))
    assert job is not None and job.attempts == 3
    jobs.complete(conn, job, datetime.now(UTC))
    done = jobs.get_job(conn, job.id)
    assert done is not None and done.state == "done"
    conn.close()
