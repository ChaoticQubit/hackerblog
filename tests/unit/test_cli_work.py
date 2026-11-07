import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from tests.integration.discovery.server import FixtureServer, post_path, serve_blogs
from tests.unit.test_pipeline import FakeBackend
from tests.unit.test_work import make_home, statuses
from typer.testing import CliRunner

from hackerblog.cli.commands import process
from hackerblog.cli.main import app
from hackerblog.core.paths import Paths

runner = CliRunner()


@pytest.fixture(scope="module")
def blogs() -> Iterator[FixtureServer]:
    with serve_blogs() as server:
        yield server


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> FakeBackend:
    fake = FakeBackend()
    monkeypatch.setattr(process, "load_backend", lambda: fake)
    return fake


def env_for(paths: Paths, tmp_path: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path),
        "HACKERBLOG_HOME": str(paths.home),
        "HACKERBLOG_CONFIG": "",
        "NO_COLOR": "1",
    }


def run(env: dict[str, str], *args: str) -> tuple[int, Any]:
    result = runner.invoke(app, list(args), env=env)
    if "--json" not in args:
        return result.exit_code, result.output
    return result.exit_code, json.loads(result.output)["data"]


def test_poll_processes_new_posts_by_default(
    tmp_path: Path, blogs: FixtureServer, backend: FakeBackend
) -> None:
    paths = make_home(tmp_path, blogs, "backfill_limit = 2")
    env = env_for(paths, tmp_path)
    code, data = run(env, "poll", "--json")
    assert code == 0, data
    assert data["processed"] == 2
    assert statuses(paths)["ready"] == 2
    assert len(list((tmp_path / "out" / "feedblog").glob("*/lesson.html"))) == 2
    calls = len(backend.calls)
    code, data = run(env, "poll", "--json")
    assert code == 0, data
    assert data["processed"] == 0 and data["tokens"] == 0
    assert len(backend.calls) == calls


def test_poll_no_process_only_queues_and_work_fetches(
    tmp_path: Path, blogs: FixtureServer, backend: FakeBackend
) -> None:
    paths = make_home(tmp_path, blogs, "backfill_limit = 1")
    env = env_for(paths, tmp_path)
    code, data = run(env, "poll", "--no-process", "--json")
    assert code == 0, data
    assert data["processed"] == 0
    assert statuses(paths).get("queued") == 1
    code, out = run(env, "work")
    assert code == 0, out
    assert "1 jobs: 1 ready" in out
    assert statuses(paths)["ready"] == 1


def test_work_without_due_jobs_needs_no_llm_key(tmp_path: Path, blogs: FixtureServer) -> None:
    paths = make_home(tmp_path, blogs)
    config = paths.config.read_text(encoding="utf-8").replace('"ollama"', '"anthropic"')
    paths.config.write_text(config, encoding="utf-8")
    env = env_for(paths, tmp_path) | {"ANTHROPIC_API_KEY": ""}
    code, out = run(env, "work")
    assert code == 0, out
    assert "No jobs are due." in out


def test_process_url_prints_the_lesson(
    tmp_path: Path, blogs: FixtureServer, backend: FakeBackend
) -> None:
    paths = make_home(tmp_path, blogs)
    env = env_for(paths, tmp_path)
    url = blogs.base_url + post_path("feedblog", "dns-rebinding-explained")
    code, data = run(env, "process", url, "--json")
    assert code == 0, data
    assert data["status"] == "ready" and data["site"] == "feedblog"
    assert Path(data["lesson"]).is_file()
    code, out = run(env, "process", url)
    assert code == 0, out
    assert "ready" in out and "0.0k tokens" in out
    code, data = run(env, "process", url, "--no-wait", "--json")
    assert code == 0, data
    assert data["status"] == "queued"


def test_process_refused_url_exits_9(tmp_path: Path, blogs: FixtureServer) -> None:
    paths = make_home(tmp_path, blogs)
    code, out = run(env_for(paths, tmp_path), "process", "http://10.0.0.1/post")
    assert code == 9, out


def test_backfill_estimates_then_processes(
    tmp_path: Path, blogs: FixtureServer, backend: FakeBackend
) -> None:
    paths = make_home(tmp_path, blogs, "backfill_limit = 1")
    env = env_for(paths, tmp_path)
    code, data = run(env, "poll", "--no-process", "--json")
    assert code == 0, data
    code, data = run(env, "backfill", "feedblog", "--limit", "3", "--dry-run", "--json")
    assert code == 0, data
    assert len(data["queued"]) == 2 and data["processed"] == 0
    assert data["estimate_tokens"] == 2 * 40_000
    code, out = run(env, "backfill", "feedblog", "--limit", "3")
    assert code == 0, out
    assert "Queuing 2 posts, estimated 80k tokens." in out
    assert statuses(paths)["ready"] == 3


def test_backfill_unknown_site_exits_4(tmp_path: Path, blogs: FixtureServer) -> None:
    paths = make_home(tmp_path, blogs)
    code, out = run(env_for(paths, tmp_path), "backfill", "nope")
    assert code == 4, out
