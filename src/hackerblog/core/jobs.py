"""SQLite job queue for process_post jobs: enqueue, atomic claim with a lease, retry backoff."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from hackerblog.core.store.repo import iso, set_post_status, transaction

__all__ = [
    "Job",
    "LeaseLostError",
    "cancel",
    "claim",
    "complete",
    "defer",
    "enqueue",
    "fail",
    "get_job",
    "iso",
    "release",
    "renew_lease",
    "retry",
]

MAX_ATTEMPTS = 5
LEASE_S = 600
BACKOFF = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=2),
    timedelta(hours=12),
)
STAGES = frozenset({"all", "lesson", "render"})
EXPIRE_SQL = (
    "UPDATE jobs SET state = 'dead', lease_until = NULL, last_error = 'lease expired',"
    " updated_at = :now WHERE state = 'running' AND lease_until < :now"
    " AND attempts >= max_attempts RETURNING post_id"
)
CLAIM_SQL = (
    "UPDATE jobs SET state = 'running', attempts = attempts + 1, lease_until = :lease,"
    " updated_at = :now WHERE id = (SELECT id FROM jobs"
    " WHERE (state = 'pending' AND run_after <= :now)"
    " OR (state = 'running' AND lease_until < :now AND attempts < max_attempts)"
    " ORDER BY run_after, id LIMIT 1) RETURNING *"
)


@dataclass(frozen=True)
class Job:
    """One jobs row. Times are UTC ISO-8601 strings ending in Z."""

    id: int
    kind: str
    post_id: int | None
    notification_id: int | None
    stage: str
    state: str
    attempts: int
    max_attempts: int
    run_after: str
    lease_until: str | None
    last_error: str | None
    created_at: str
    updated_at: str


def get_job(conn: sqlite3.Connection, job_id: int) -> Job | None:
    """Return the job with job_id, or None."""
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return None if row is None else Job(*row)


def enqueue(conn: sqlite3.Connection, post_id: int, stage: str, now: datetime) -> int:
    """Add a pending process_post job for post_id due now and return its id.

    If the post already has a pending or running job, return that job's id instead.
    """
    assert stage in STAGES, f"unknown stage {stage!r}"
    stamp = iso(now)
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO jobs (kind, post_id, stage, state, max_attempts, run_after,"
            " created_at, updated_at) VALUES ('process_post', ?, ?, 'pending', ?, ?, ?, ?)"
            " ON CONFLICT DO NOTHING",
            (post_id, stage, MAX_ATTEMPTS, stamp, stamp, stamp),
        )
        if cur.rowcount == 1:
            assert cur.lastrowid is not None
            return cur.lastrowid
        row = conn.execute(
            "SELECT id FROM jobs WHERE post_id = ? AND kind = 'process_post'"
            " AND state IN ('pending', 'running')",
            (post_id,),
        ).fetchone()
    assert row is not None, f"no open job after conflict for post {post_id}"
    return int(row[0])


def claim(conn: sqlite3.Connection, now: datetime, lease_s: int = LEASE_S) -> Job | None:
    """Atomically take the next due pending job, or a running one whose lease expired.

    Counts an attempt and sets the lease. A running job whose lease expired on its last
    attempt goes dead with its post instead. Returns None when nothing is due.
    """
    assert lease_s > 0
    lease = iso(now + timedelta(seconds=lease_s))
    with transaction(conn):
        for (post_id,) in conn.execute(EXPIRE_SQL, {"now": iso(now)}).fetchall():
            if post_id is not None:
                set_post_status(conn, int(post_id), "dead", error="lease expired")
        rows = conn.execute(CLAIM_SQL, {"now": iso(now), "lease": lease}).fetchall()
    if not rows:
        return None
    assert len(rows) == 1
    job = Job(*rows[0])
    assert job.state == "running" and job.lease_until == lease
    return job


class LeaseLostError(Exception):
    """The job is no longer this run's: its lease expired and it was reclaimed, or it finished."""


FENCE = " WHERE id = ? AND state = 'running' AND attempts = ?"


def _fenced(conn: sqlite3.Connection, job: Job, sql: str, params: tuple[object, ...]) -> None:
    assert job.state == "running" and job.attempts >= 1, f"job {job.id} was not claimed"
    assert sql.startswith("UPDATE jobs SET ") and sql.endswith(FENCE)
    cur = conn.execute(sql, (*params, job.id, job.attempts))
    if cur.rowcount == 0:
        raise LeaseLostError(f"job {job.id} attempt {job.attempts} lost its lease")
    assert cur.rowcount == 1


def renew_lease(conn: sqlite3.Connection, job: Job, now: datetime, lease_s: int = LEASE_S) -> None:
    """Extend this run's lease to now plus lease_s seconds. Raises LeaseLostError."""
    assert lease_s > 0
    lease = iso(now + timedelta(seconds=lease_s))
    _fenced(
        conn,
        job,
        "UPDATE jobs SET lease_until = ?, updated_at = ?"
        " WHERE id = ? AND state = 'running' AND attempts = ?",
        (lease, iso(now)),
    )


def complete(conn: sqlite3.Connection, job: Job, now: datetime) -> None:
    """Mark this run's job done and clear its lease. Raises LeaseLostError."""
    _fenced(
        conn,
        job,
        "UPDATE jobs SET state = 'done', lease_until = NULL, updated_at = ?"
        " WHERE id = ? AND state = 'running' AND attempts = ?",
        (iso(now),),
    )


def release(conn: sqlite3.Connection, job: Job, now: datetime) -> None:
    """Put this run's job back to pending, due now, without counting the attempt (Ctrl-C).

    Raises LeaseLostError.
    """
    defer(conn, job, now, now)


def defer(conn: sqlite3.Connection, job: Job, until: datetime, now: datetime) -> None:
    """Put this run's job back to pending, due at until, without counting the attempt.

    Clears the lease. Raises LeaseLostError.
    """
    assert until >= now, "defer into the past"
    _fenced(
        conn,
        job,
        "UPDATE jobs SET state = 'pending', attempts = attempts - 1, lease_until = NULL,"
        " run_after = ?, updated_at = ? WHERE id = ? AND state = 'running' AND attempts = ?",
        (iso(until), iso(now)),
    )


def fail(conn: sqlite3.Connection, job: Job, error: str, now: datetime) -> str:
    """Record this run's failed attempt and return the job's new state.

    'pending' with run_after pushed by the backoff for the attempt, or 'dead' once attempts
    reach max_attempts. Raises LeaseLostError.
    """
    delay = BACKOFF[min(job.attempts, len(BACKOFF)) - 1]
    state = "dead" if job.attempts >= job.max_attempts else "pending"
    _fenced(
        conn,
        job,
        "UPDATE jobs SET state = ?, run_after = ?, lease_until = NULL, last_error = ?,"
        " updated_at = ? WHERE id = ? AND state = 'running' AND attempts = ?",
        (state, iso(now + delay), error, iso(now)),
    )
    return state


def retry(conn: sqlite3.Connection, job_id: int, now: datetime) -> bool:
    """Move a failed or dead job back to pending, due now, attempts reset; requeue its post.

    Returns False if the job is unknown, not failed or dead, or its post has an open job.
    """
    stamp = iso(now)
    with transaction(conn):
        job = get_job(conn, job_id)
        if job is None or job.state not in {"failed", "dead"}:
            return False
        cur = conn.execute(
            "UPDATE OR IGNORE jobs SET state = 'pending', attempts = 0, run_after = ?,"
            " lease_until = NULL, last_error = NULL, updated_at = ? WHERE id = ?",
            (stamp, stamp, job_id),
        )
        if cur.rowcount == 0:
            return False
        if job.post_id is not None:
            set_post_status(conn, job.post_id, "queued")
    assert job.state in {"failed", "dead"}
    return True


def cancel(conn: sqlite3.Connection, job_id: int, now: datetime) -> bool:
    """Cancel a pending or failed job and skip its post with reason 'cancelled'.

    Returns False if the job is unknown or in any other state.
    """
    with transaction(conn):
        cur = conn.execute(
            "UPDATE jobs SET state = 'cancelled', lease_until = NULL, updated_at = ?"
            " WHERE id = ? AND state IN ('pending', 'failed') RETURNING post_id",
            (iso(now), job_id),
        )
        rows = cur.fetchall()
        if not rows:
            return False
        if rows[0][0] is not None:
            set_post_status(conn, int(rows[0][0]), "skipped", skip_reason="cancelled")
    assert len(rows) == 1
    return True
