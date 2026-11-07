import asyncio
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.integration.discovery.server import FixtureServer, post_path, serve_blogs
from tests.unit.test_pipeline import FakeBackend

from hackerblog.core import service, work
from hackerblog.core.discovery import service as discovery
from hackerblog.core.errors import ExitCode, HackerBlogError
from hackerblog.core.paths import Paths, resolve_paths


@pytest.fixture(scope="module")
def blogs() -> Iterator[FixtureServer]:
    with serve_blogs() as server:
        yield server


def make_home(tmp_path: Path, blogs: FixtureServer, site_lines: str = "") -> Paths:
    paths = resolve_paths(home=tmp_path / "hb", env={})
    service.init_home(paths, tmp_path / "out", "ollama", force=False)
    paths.config.write_text(
        "\n".join(
            [
                "version = 1",
                "[llm]",
                'provider = "ollama"',
                'notes_model = "notes-model"',
                'lesson_model = "lesson-model"',
                "[output]",
                f"dir = {json.dumps(str(tmp_path / 'out'))}",
                "[fetch]",
                "min_delay_seconds = 0.5",
                'allow_private_hosts = ["127.0.0.1"]',
                "[[site]]",
                f'url = "{blogs.base_url}/feedblog/"',
                'slug = "feedblog"',
                site_lines,
            ]
        ),
        encoding="utf-8",
    )
    return paths


def statuses(paths: Paths) -> dict[str, int]:
    conn = sqlite3.connect(paths.db)
    try:
        rows = conn.execute("SELECT status, COUNT(*) FROM posts GROUP BY status").fetchall()
    finally:
        conn.close()
    return dict(rows)


def test_run_queue_fetches_queued_posts_with_the_real_fetcher(
    tmp_path: Path, blogs: FixtureServer
) -> None:
    paths = make_home(tmp_path, blogs, "backfill_limit = 2")
    asyncio.run(discovery.poll(paths))
    assert statuses(paths).get("queued") == 2
    report = asyncio.run(work.run_queue(paths, FakeBackend()))
    assert report.stop is None
    assert [result.state for result in report.results] == ["ready", "ready"]
    assert statuses(paths)["ready"] == 2
    lessons = list((tmp_path / "out" / "feedblog").glob("*/lesson.html"))
    assert len(lessons) == 2


def test_process_url_matches_the_site_and_is_free_the_second_time(
    tmp_path: Path, blogs: FixtureServer
) -> None:
    paths = make_home(tmp_path, blogs)
    url = blogs.base_url + post_path("feedblog", "dns-rebinding-explained")
    backend = FakeBackend()
    first = asyncio.run(work.process_url(paths, backend, url))
    assert first.outcome is not None and first.outcome.status == "ready"
    assert first.site == "feedblog"
    calls = len(backend.calls)
    again = asyncio.run(work.process_url(paths, backend, url))
    assert again.post_id == first.post_id
    assert again.outcome is not None and again.outcome.tokens == 0
    assert len(backend.calls) == calls


def test_process_url_without_wait_only_queues(tmp_path: Path, blogs: FixtureServer) -> None:
    paths = make_home(tmp_path, blogs)
    url = blogs.base_url + post_path("feedblog", "csp-bypasses-in-practice")
    queued = asyncio.run(work.process_url(paths, FakeBackend(), url, wait=False))
    assert queued.outcome is None
    assert statuses(paths) == {"queued": 1}


def test_process_url_refuses_a_private_address(tmp_path: Path, blogs: FixtureServer) -> None:
    paths = make_home(tmp_path, blogs)
    with pytest.raises(HackerBlogError) as caught:
        asyncio.run(work.process_url(paths, FakeBackend(), "http://10.0.0.1/post"))
    assert caught.value.code == ExitCode.BLOCKED


def test_process_url_with_an_unknown_site_is_not_found(
    tmp_path: Path, blogs: FixtureServer
) -> None:
    paths = make_home(tmp_path, blogs)
    url = blogs.base_url + post_path("feedblog", "csp-bypasses-in-practice")
    with pytest.raises(HackerBlogError) as caught:
        asyncio.run(work.process_url(paths, FakeBackend(), url, site="nope"))
    assert caught.value.code == ExitCode.NOT_FOUND


def test_backfill_requeues_skipped_posts_up_to_the_limit(
    tmp_path: Path, blogs: FixtureServer
) -> None:
    paths = make_home(tmp_path, blogs, "backfill_limit = 1")
    asyncio.run(discovery.poll(paths))
    asyncio.run(work.run_queue(paths, FakeBackend()))
    skipped = statuses(paths)["skipped"]
    plan = asyncio.run(work.backfill(paths, "feedblog", limit=3))
    assert len(plan.queued) == 2
    assert plan.estimate_tokens == 2 * work.TOKENS_PER_POST_ESTIMATE
    assert statuses(paths) == {"ready": 1, "queued": 2, "skipped": skipped - 2}


def test_backfill_dry_run_writes_nothing(tmp_path: Path, blogs: FixtureServer) -> None:
    paths = make_home(tmp_path, blogs, "backfill_limit = 1")
    asyncio.run(discovery.poll(paths))
    before = statuses(paths)
    plan = asyncio.run(work.backfill(paths, "feedblog", limit=50, dry_run=True))
    assert len(plan.queued) == before["skipped"]
    assert statuses(paths) == before


def test_backfill_unknown_site_is_not_found(tmp_path: Path, blogs: FixtureServer) -> None:
    paths = make_home(tmp_path, blogs)
    with pytest.raises(HackerBlogError) as caught:
        asyncio.run(work.backfill(paths, "nope", limit=None))
    assert caught.value.code == ExitCode.NOT_FOUND
