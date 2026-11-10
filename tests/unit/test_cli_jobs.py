import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hackerblog.cli.main import app
from hackerblog.core import jobs
from hackerblog.core.store import repo
from hackerblog.core.store.migrate import connect, migrate

runner = CliRunner()
T0 = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    values = {
        "HOME": str(tmp_path),
        "HACKERBLOG_HOME": str(tmp_path / "hb"),
        "HACKERBLOG_CONFIG": "",
        "ANTHROPIC_API_KEY": "",
        "NO_COLOR": "1",
    }
    code, out = invoke(values, "init", "--output-dir", str(tmp_path / "out"))
    assert code == 0, out
    return values


@pytest.fixture
def conn(env: dict[str, str]) -> Iterator[sqlite3.Connection]:
    db = connect(Path(env["HACKERBLOG_HOME"]) / "hackerblog.db")
    migrate(db)
    yield db
    db.close()


def invoke(env: dict[str, str], *args: str) -> tuple[int, str]:
    result = runner.invoke(app, list(args), env=env)
    return result.exit_code, result.output


def job_for(conn: sqlite3.Connection, n: int, state: str) -> tuple[int, int]:
    site_id = repo.ensure_adhoc_site(conn)
    url = f"https://x.example/p/{n}"
    post_id, created = repo.insert_manual_post(conn, site_id, url, url)
    assert created
    job_id = jobs.enqueue(conn, post_id, "all", T0)
    if state != "pending":
        conn.execute(
            "UPDATE jobs SET state = ?, attempts = 5, last_error = 'boom' WHERE id = ?",
            (state, job_id),
        )
        repo.set_post_status(conn, post_id, "dead", error="boom")
    return job_id, post_id


def state_of(conn: sqlite3.Connection, job_id: int) -> tuple[str, int]:
    row = conn.execute("SELECT state, attempts FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return row[0], row[1]


def post_status(conn: sqlite3.Connection, post_id: int) -> str:
    return conn.execute("SELECT status FROM posts WHERE id = ?", (post_id,)).fetchone()[0]


def test_retry_one_job(env: dict[str, str], conn: sqlite3.Connection) -> None:
    job_id, post_id = job_for(conn, 1, "dead")
    other, _ = job_for(conn, 2, "failed")
    code, out = invoke(env, "jobs", "retry", str(job_id))
    assert code == 0, out
    assert f"job {job_id}" in out
    assert state_of(conn, job_id) == ("pending", 0)
    assert post_status(conn, post_id) == "queued"
    assert state_of(conn, other)[0] == "failed"


def test_retry_all_failed_and_dead(env: dict[str, str], conn: sqlite3.Connection) -> None:
    dead, dead_post = job_for(conn, 1, "dead")
    failed, failed_post = job_for(conn, 2, "failed")
    pending, _ = job_for(conn, 3, "pending")
    code, out = invoke(env, "jobs", "retry", "--json")
    assert code == 0, out
    assert json.loads(out)["data"] == {"retried": [dead, failed]}
    assert state_of(conn, dead) == ("pending", 0)
    assert state_of(conn, failed) == ("pending", 0)
    assert post_status(conn, dead_post) == post_status(conn, failed_post) == "queued"
    assert state_of(conn, pending) == ("pending", 0)


def test_retry_unknown_id_is_not_found(env: dict[str, str]) -> None:
    code, out = invoke(env, "jobs", "retry", "9999")
    assert code == 4, out
    assert "9999" in out


def test_retry_open_job_is_usage(env: dict[str, str], conn: sqlite3.Connection) -> None:
    pending, _ = job_for(conn, 1, "pending")
    code, out = invoke(env, "jobs", "retry", str(pending))
    assert code == 2, out
    assert "pending" in out


def test_retry_nothing_to_retry(env: dict[str, str], conn: sqlite3.Connection) -> None:
    job_for(conn, 1, "pending")
    code, out = invoke(env, "jobs", "retry")
    assert code == 0, out
    assert "No failed or dead jobs" in out
