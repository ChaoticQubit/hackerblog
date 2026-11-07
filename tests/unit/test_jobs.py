import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from hackerblog.core import jobs
from hackerblog.core.store import repo
from hackerblog.core.store.migrate import connect, migrate

T0 = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = connect(tmp_path / "hb.db")
    migrate(db)
    yield db
    db.close()


def new_post(conn: sqlite3.Connection, n: int = 1) -> int:
    site_id = repo.ensure_adhoc_site(conn)
    url = f"https://x.example/p/{n}"
    post_id, created = repo.insert_manual_post(conn, site_id, url, url)
    assert created
    return post_id


def test_iso_is_utc_z() -> None:
    assert jobs.iso(T0) == "2026-11-06T12:00:00Z"
    assert jobs.iso(T0.astimezone(timezone(timedelta(hours=2)))) == "2026-11-06T12:00:00Z"


def test_enqueue_returns_existing_open_job(conn: sqlite3.Connection) -> None:
    post_id = new_post(conn)
    first = jobs.enqueue(conn, post_id, "all", T0)
    assert jobs.enqueue(conn, post_id, "render", T0) == first
    job = jobs.get_job(conn, first)
    assert job is not None
    assert (job.kind, job.state, job.stage, job.attempts, job.max_attempts) == (
        "process_post",
        "pending",
        "all",
        0,
        5,
    )
    assert job.run_after == "2026-11-06T12:00:00Z"


def test_claim_takes_due_job_once(conn: sqlite3.Connection) -> None:
    post_id = new_post(conn)
    job_id = jobs.enqueue(conn, post_id, "all", T0)
    assert jobs.claim(conn, T0 - timedelta(seconds=1)) is None
    job = jobs.claim(conn, T0)
    assert job is not None
    assert job.id == job_id
    assert job.state == "running"
    assert job.attempts == 1
    assert job.lease_until == "2026-11-06T12:10:00Z"
    assert jobs.claim(conn, T0) is None


def test_claim_orders_by_run_after(conn: sqlite3.Connection) -> None:
    late = jobs.enqueue(conn, new_post(conn, 1), "all", T0)
    early = jobs.enqueue(conn, new_post(conn, 2), "all", T0 - timedelta(minutes=5))
    first = jobs.claim(conn, T0)
    second = jobs.claim(conn, T0)
    assert first is not None and second is not None
    assert (first.id, second.id) == (early, late)


def test_expired_lease_is_reclaimed(conn: sqlite3.Connection) -> None:
    job_id = jobs.enqueue(conn, new_post(conn), "all", T0)
    assert jobs.claim(conn, T0, lease_s=60) is not None
    assert jobs.claim(conn, T0 + timedelta(seconds=59)) is None
    again = jobs.claim(conn, T0 + timedelta(seconds=61))
    assert again is not None
    assert again.id == job_id
    assert again.attempts == 2


def test_renew_lease_extends(conn: sqlite3.Connection) -> None:
    job_id = jobs.enqueue(conn, new_post(conn), "all", T0)
    running = jobs.claim(conn, T0, lease_s=60)
    assert running is not None
    jobs.renew_lease(conn, running, T0 + timedelta(seconds=50), lease_s=60)
    assert jobs.claim(conn, T0 + timedelta(seconds=61)) is None
    job = jobs.get_job(conn, job_id)
    assert job is not None
    assert job.lease_until == "2026-11-06T12:01:50Z"


def test_complete(conn: sqlite3.Connection) -> None:
    post_id = new_post(conn)
    job_id = jobs.enqueue(conn, post_id, "all", T0)
    running = jobs.claim(conn, T0)
    assert running is not None
    jobs.complete(conn, running, T0)
    job = jobs.get_job(conn, job_id)
    assert job is not None
    assert (job.state, job.lease_until) == ("done", None)
    assert jobs.enqueue(conn, post_id, "render", T0) != job_id


def test_fail_backs_off_then_dies(conn: sqlite3.Connection) -> None:
    job_id = jobs.enqueue(conn, new_post(conn), "all", T0)
    now = T0
    expected = [1, 5, 30, 120]
    for minutes in expected:
        running = jobs.claim(conn, now)
        assert running is not None
        assert jobs.fail(conn, running, "boom", now) == "pending"
        job = jobs.get_job(conn, job_id)
        assert job is not None
        assert job.run_after == jobs.iso(now + timedelta(minutes=minutes))
        assert job.last_error == "boom"
        assert jobs.claim(conn, now + timedelta(minutes=minutes) - timedelta(seconds=1)) is None
        now += timedelta(minutes=minutes)
    running = jobs.claim(conn, now)
    assert running is not None
    assert jobs.fail(conn, running, "boom", now) == "dead"
    assert jobs.claim(conn, now + timedelta(days=2)) is None


def test_release_does_not_count_attempt(conn: sqlite3.Connection) -> None:
    job_id = jobs.enqueue(conn, new_post(conn), "all", T0)
    running = jobs.claim(conn, T0)
    assert running is not None
    jobs.release(conn, running, T0)
    job = jobs.get_job(conn, job_id)
    assert job is not None
    assert (job.state, job.attempts, job.lease_until) == ("pending", 0, None)
    assert jobs.claim(conn, T0) is not None


def dead_job(conn: sqlite3.Connection, post_id: int) -> int:
    job_id = jobs.enqueue(conn, post_id, "all", T0)
    conn.execute("UPDATE jobs SET max_attempts = 1 WHERE id = ?", (job_id,))
    running = jobs.claim(conn, T0)
    assert running is not None
    assert jobs.fail(conn, running, "boom", T0) == "dead"
    return job_id


def test_retry_dead_job(conn: sqlite3.Connection) -> None:
    post_id = new_post(conn)
    job_id = dead_job(conn, post_id)
    repo.set_post_status(conn, post_id, "dead", error="boom")
    later = T0 + timedelta(hours=1)
    assert jobs.retry(conn, job_id, later)
    job = jobs.get_job(conn, job_id)
    assert job is not None
    assert (job.state, job.attempts, job.run_after, job.last_error) == (
        "pending",
        0,
        jobs.iso(later),
        None,
    )
    post = repo.get_post(conn, post_id)
    assert post is not None
    assert post.status == "queued"


def test_retry_refuses_pending_and_open_duplicate(conn: sqlite3.Connection) -> None:
    post_id = new_post(conn)
    job_id = dead_job(conn, post_id)
    other = jobs.enqueue(conn, post_id, "all", T0)
    assert not jobs.retry(conn, job_id, T0)
    assert not jobs.retry(conn, other, T0)
    assert not jobs.retry(conn, 9999, T0)


def test_cancel_pending_skips_post(conn: sqlite3.Connection) -> None:
    post_id = new_post(conn)
    job_id = jobs.enqueue(conn, post_id, "all", T0)
    assert jobs.cancel(conn, job_id, T0)
    job = jobs.get_job(conn, job_id)
    assert job is not None
    assert job.state == "cancelled"
    post = repo.get_post(conn, post_id)
    assert post is not None
    assert (post.status, post.skip_reason) == ("skipped", "cancelled")
    assert not jobs.cancel(conn, job_id, T0)


def test_cancel_refuses_running(conn: sqlite3.Connection) -> None:
    job_id = jobs.enqueue(conn, new_post(conn), "all", T0)
    assert jobs.claim(conn, T0) is not None
    assert not jobs.cancel(conn, job_id, T0)


def test_stale_run_loses_lease_after_reclaim(conn: sqlite3.Connection) -> None:
    job_id = jobs.enqueue(conn, new_post(conn), "all", T0)
    stale = jobs.claim(conn, T0, lease_s=60)
    assert stale is not None
    later = T0 + timedelta(seconds=61)
    fresh = jobs.claim(conn, later, lease_s=60)
    assert fresh is not None and fresh.id == job_id and fresh.attempts == 2
    with pytest.raises(jobs.LeaseLostError):
        jobs.complete(conn, stale, later)
    with pytest.raises(jobs.LeaseLostError):
        jobs.fail(conn, stale, "late", later)
    with pytest.raises(jobs.LeaseLostError):
        jobs.renew_lease(conn, stale, later)
    with pytest.raises(jobs.LeaseLostError):
        jobs.release(conn, stale, later)
    with pytest.raises(jobs.LeaseLostError):
        jobs.defer(conn, stale, later + timedelta(hours=1), later)
    assert jobs.get_job(conn, job_id) == fresh
    jobs.complete(conn, fresh, later)
    done = jobs.get_job(conn, job_id)
    assert done is not None and done.state == "done"


def test_finished_job_rejects_second_complete(conn: sqlite3.Connection) -> None:
    jobs.enqueue(conn, new_post(conn), "all", T0)
    running = jobs.claim(conn, T0)
    assert running is not None
    jobs.complete(conn, running, T0)
    with pytest.raises(jobs.LeaseLostError):
        jobs.complete(conn, running, T0)


def test_expired_lease_on_last_attempt_goes_dead(conn: sqlite3.Connection) -> None:
    post_id = new_post(conn)
    job_id = jobs.enqueue(conn, post_id, "all", T0)
    conn.execute("UPDATE jobs SET max_attempts = 1 WHERE id = ?", (job_id,))
    assert jobs.claim(conn, T0, lease_s=60) is not None
    assert jobs.claim(conn, T0 + timedelta(seconds=61)) is None
    job = jobs.get_job(conn, job_id)
    assert job is not None
    assert (job.state, job.attempts, job.lease_until, job.last_error) == (
        "dead",
        1,
        None,
        "lease expired",
    )
    post = repo.get_post(conn, post_id)
    assert post is not None
    assert (post.status, post.last_error) == ("dead", "lease expired")
    assert jobs.retry(conn, job_id, T0 + timedelta(minutes=5))


def test_defer_does_not_count_attempt(conn: sqlite3.Connection) -> None:
    job_id = jobs.enqueue(conn, new_post(conn), "all", T0)
    running = jobs.claim(conn, T0)
    assert running is not None
    until = T0 + timedelta(hours=3)
    jobs.defer(conn, running, until, T0)
    job = jobs.get_job(conn, job_id)
    assert job is not None
    assert (job.state, job.attempts, job.lease_until, job.run_after) == (
        "pending",
        0,
        None,
        jobs.iso(until),
    )
    assert jobs.claim(conn, until - timedelta(seconds=1)) is None
    again = jobs.claim(conn, until)
    assert again is not None and again.attempts == 1
