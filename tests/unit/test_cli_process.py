import asyncio
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.test_pipeline import FakeBackend, add_post, race_pages
from typer.testing import CliRunner

from hackerblog.cli.commands import process
from hackerblog.cli.main import app
from hackerblog.core import jobs, pipeline
from hackerblog.core.paths import resolve_paths
from hackerblog.core.store import repo
from hackerblog.core.store.migrate import connect, migrate

runner = CliRunner()


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> FakeBackend:
    fake = FakeBackend()
    monkeypatch.setattr(process, "load_backend", lambda: fake)
    return fake


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = tmp_path / "hb"
    home.mkdir(mode=0o700)
    config = home / "sites.toml"
    config.write_text(
        'version = 1\n[llm]\nprovider = "ollama"\nnotes_model = "notes-model"\n'
        'lesson_model = "lesson-model"\n'
        f'[output]\ndir = "{tmp_path / "out"}"\n'
    )
    config.chmod(0o600)
    return home


@pytest.fixture
def env(home: Path, tmp_path: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path),
        "HACKERBLOG_HOME": str(home),
        "HACKERBLOG_CONFIG": "",
        "NO_COLOR": "1",
    }


@pytest.fixture
def conn(home: Path) -> Iterator[sqlite3.Connection]:
    db = connect(home / "hackerblog.db")
    migrate(db)
    yield db
    db.close()


def ready_post(conn: sqlite3.Connection, home: Path) -> int:
    post_id = add_post(conn)
    settings = pipeline.load_settings(resolve_paths(home, None, env={}), need_llm=True, env={})
    asyncio.run(pipeline.process_post(conn, post_id, settings, FakeBackend(), fetcher=race_pages()))
    return post_id


def invoke(env: dict[str, str], *args: str) -> tuple[int, str]:
    result = runner.invoke(app, list(args), env=env)
    return result.exit_code, result.output


def lesson_of(conn: sqlite3.Connection, post_id: int) -> Path:
    return next(Path(a.path) for a in repo.artifacts_for(conn, post_id) if a.kind == "lesson")


def test_render_rewrites_every_ready_post_without_llm(
    env: dict[str, str], conn: sqlite3.Connection, home: Path, backend: FakeBackend
) -> None:
    post_id = ready_post(conn, home)
    lesson = lesson_of(conn, post_id)
    lesson.unlink()
    code, out = invoke(env, "render", "--json")
    assert code == 0, out
    payload = json.loads(out)["data"]
    assert payload["rendered"] == 1 and payload["failed"] == []
    assert lesson.is_file()
    assert backend.calls == []


def test_render_unknown_site_is_not_found(env: dict[str, str], conn: sqlite3.Connection) -> None:
    code, out = invoke(env, "render", "--site", "nope")
    assert code == 4, out


def test_regenerate_lesson_prints_the_lesson_path(
    env: dict[str, str], conn: sqlite3.Connection, home: Path, backend: FakeBackend
) -> None:
    post_id = ready_post(conn, home)
    code, out = invoke(env, "regenerate", str(post_id), "--stage", "lesson")
    assert code == 0, out
    assert backend.calls == ["lesson"]
    assert str(lesson_of(conn, post_id)) in out
    assert "2.0k tokens" in out


def test_regenerate_unknown_post_exits_4(
    env: dict[str, str], conn: sqlite3.Connection, backend: FakeBackend
) -> None:
    code, out = invoke(env, "regenerate", "999", "--json")
    assert code == 4, out
    assert json.loads(out)["error"]["code"] == "not_found"


def test_regenerate_bad_stage_is_usage(env: dict[str, str]) -> None:
    code, _ = invoke(env, "regenerate", "1", "--stage", "everything")
    assert code == 2


def test_work_runs_queued_render_jobs(
    env: dict[str, str], conn: sqlite3.Connection, home: Path, backend: FakeBackend
) -> None:
    post_id = ready_post(conn, home)
    jobs.enqueue(conn, post_id, "render", datetime.now(UTC))
    code, out = invoke(env, "work", "--json")
    assert code == 0, out
    data = json.loads(out)["data"]
    assert [job["state"] for job in data["jobs"]] == ["ready"]
    assert data["tokens"] == 0


def test_work_stops_on_a_fetch_job_this_build_cannot_run(
    env: dict[str, str], conn: sqlite3.Connection, backend: FakeBackend
) -> None:
    post_id = add_post(conn)
    jobs.enqueue(conn, post_id, "all", datetime.now(UTC))
    code, out = invoke(env, "work")
    assert code == 1, out
    job = jobs.get_job(conn, 1)
    assert job is not None and job.state == "pending" and job.attempts == 0


def test_work_concurrency_is_bounded(env: dict[str, str], backend: FakeBackend) -> None:
    code, _ = invoke(env, "work", "--concurrency", "9")
    assert code == 2


def test_work_empty_queue(
    env: dict[str, str], conn: sqlite3.Connection, backend: FakeBackend
) -> None:
    code, out = invoke(env, "work")
    assert code == 0, out
    assert "No jobs" in out
