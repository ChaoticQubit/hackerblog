import asyncio
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
from tests.samples import RACE_POST_URL
from tests.unit.test_pipeline import FakeBackend, Pages, add_post, race_pages, settings

from hackerblog.core import jobs, pipeline
from hackerblog.core.errors import ExitCode, HackerBlogError
from hackerblog.core.llm.schemas import Generated, LessonSpec, PostContext, PostDigest
from hackerblog.core.llm.settings import LLMSettings
from hackerblog.core.store import repo
from hackerblog.core.store.migrate import connect, migrate


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = connect(tmp_path / "hb.db")
    migrate(db)
    yield db
    db.close()


def queue(conn: sqlite3.Connection, url: str = RACE_POST_URL, stage: str = "all") -> int:
    post_id = add_post(conn, url)
    jobs.enqueue(conn, post_id, stage, datetime.now(UTC))
    return post_id


def work(
    conn: sqlite3.Connection,
    tmp_path: Path,
    backend: FakeBackend,
    pages: pipeline.PageFetcher | None,
    *,
    config: pipeline.Settings | None = None,
    max_jobs: int | None = None,
    concurrency: int = 1,
) -> pipeline.WorkReport:
    return asyncio.run(
        pipeline.run_worker(
            conn,
            config or settings(tmp_path),
            backend,
            fetcher=pages,
            max_jobs=max_jobs,
            concurrency=concurrency,
        )
    )


def job_of(conn: sqlite3.Connection, post_id: int) -> jobs.Job:
    row = conn.execute("SELECT id FROM jobs WHERE post_id = ?", (post_id,)).fetchone()
    job = jobs.get_job(conn, int(row[0]))
    assert job is not None
    return job


def test_worker_processes_the_queue_and_completes_jobs(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    post_id = queue(conn)
    report = work(conn, tmp_path, FakeBackend(), race_pages())
    assert [r.state for r in report.results] == ["ready"]
    assert report.tokens == 3200 and report.stop is None
    assert job_of(conn, post_id).state == "done"


class FailingBackend(FakeBackend):
    async def generate_digest(
        self, ctx: PostContext, settings: LLMSettings
    ) -> Generated[PostDigest]:
        self.calls.append("digest")
        raise HackerBlogError(ExitCode.FAILED, "provider answered 500")


def test_failure_backs_off_then_goes_dead(conn: sqlite3.Connection, tmp_path: Path) -> None:
    post_id = queue(conn)
    report = work(conn, tmp_path, FailingBackend(), race_pages())
    assert [r.state for r in report.results] == ["failed"]
    job = job_of(conn, post_id)
    assert job.state == "pending" and job.attempts == 1
    assert "provider answered 500" in (job.last_error or "")
    post = repo.get_post(conn, post_id)
    assert post is not None and post.status == "failed"
    conn.execute("UPDATE jobs SET attempts = max_attempts - 1, run_after = '2000-01-01T00:00:00Z'")
    report = work(conn, tmp_path, FailingBackend(), race_pages())
    assert [r.state for r in report.results] == ["dead"]
    post = repo.get_post(conn, post_id)
    assert post is not None and post.status == "dead"


def test_budget_stop_defers_without_counting_the_attempt(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    first = queue(conn)
    queue(conn, "https://blog.example.com/other")
    backend = FakeBackend()
    report = work(
        conn, tmp_path, backend, race_pages(), config=settings(tmp_path, daily_token_budget=1000)
    )
    assert [r.state for r in report.results] == ["deferred"]
    assert report.stop is not None and report.stop.code == ExitCode.BUDGET
    job = job_of(conn, first)
    assert job.state == "pending" and job.attempts == 0
    assert job.run_after > repo.utc_now()
    assert backend.calls == []


def test_blocked_page_skips_the_post(conn: sqlite3.Connection, tmp_path: Path) -> None:
    class Blocked:
        async def get(self, url: str, *, kind: Literal["page"]) -> pipeline.Page:
            raise HackerBlogError(ExitCode.BLOCKED, f"robots.txt disallows {url}")

    post_id = queue(conn)
    report = work(conn, tmp_path, FakeBackend(), Blocked())
    assert [r.state for r in report.results] == ["skipped"]
    post = repo.get_post(conn, post_id)
    assert post is not None and post.status == "skipped" and post.skip_reason == "blocked"
    assert job_of(conn, post_id).state == "done"


def test_auth_error_releases_the_job_and_stops(conn: sqlite3.Connection, tmp_path: Path) -> None:
    class NoKey(FakeBackend):
        async def generate_digest(
            self, ctx: PostContext, settings: LLMSettings
        ) -> Generated[PostDigest]:
            raise HackerBlogError(ExitCode.LLM_AUTH, "key rejected")

    post_id = queue(conn)
    queue(conn, "https://blog.example.com/other")
    report = work(conn, tmp_path, NoKey(), race_pages())
    assert report.stop is not None and report.stop.code == ExitCode.LLM_AUTH
    assert report.results == []
    job = job_of(conn, post_id)
    assert job.state == "pending" and job.attempts == 0
    post = repo.get_post(conn, post_id)
    assert post is not None and post.status == "queued"


def test_lost_lease_writes_nothing_more(conn: sqlite3.Connection, tmp_path: Path) -> None:
    class Slow(FakeBackend):
        async def generate_lesson(
            self, ctx: PostContext, digest: PostDigest, settings: LLMSettings
        ) -> Generated[LessonSpec]:
            stale = datetime.now(UTC) + timedelta(seconds=jobs.LEASE_S + 1)
            conn.execute("UPDATE jobs SET lease_until = '2000-01-01T00:00:00Z'")
            other = jobs.claim(conn, stale)
            assert other is not None
            return await super().generate_lesson(ctx, digest, settings)

    post_id = queue(conn)
    report = work(conn, tmp_path, Slow(), race_pages())
    assert [r.state for r in report.results] == ["lost"]
    assert job_of(conn, post_id).attempts == 2


def test_max_jobs_and_concurrency(conn: sqlite3.Connection, tmp_path: Path) -> None:
    urls = [f"https://blog.example.com/p{n}" for n in range(3)]
    pages = Pages({url: race_pages().pages[RACE_POST_URL] for url in urls})
    for url in urls:
        queue(conn, url)
    report = work(conn, tmp_path, FakeBackend(), pages, max_jobs=2, concurrency=2)
    assert len(report.results) == 2
    states = [row[0] for row in conn.execute("SELECT state FROM jobs ORDER BY id")]
    assert sorted(states) == ["done", "done", "pending"]


def test_stage_all_job_without_fetcher_is_released(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    post_id = queue(conn)
    report = work(conn, tmp_path, FakeBackend(), None)
    assert report.stop is not None and report.stop.code == ExitCode.INTERNAL
    assert job_of(conn, post_id).state == "pending"
