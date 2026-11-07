import http.client
import threading
import time
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI

from hackerblog.api.app import ServiceSettings, create_app
from hackerblog.core.config import Config
from hackerblog.core.paths import resolve_paths
from hackerblog.core.store.migrate import connect, migrate

TOKEN = "integration-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SLUG = "a-example-blog"


def make_settings(tmp_path: Path) -> ServiceSettings:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    out = tmp_path / "out"
    (out / "site").mkdir(parents=True)
    (out / "site" / "lesson.html").write_text("<p>ok</p>", encoding="utf-8")
    (tmp_path / "secret.html").write_text("outside", encoding="utf-8")
    paths = resolve_paths(home=home)
    conn = connect(paths.db)
    migrate(conn)
    conn.close()
    config = Config.model_validate(
        {
            "version": 1,
            "output": {"dir": str(out)},
            "notify": {"timezone": "UTC"},
            "site": [{"url": "https://a.example/blog"}],
        }
    )
    return ServiceSettings(token=TOKEN, paths=paths, config=config)


@contextmanager
def serving(app: FastAPI) -> Generator[tuple[str, int]]:
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        assert thread.is_alive(), "uvicorn exited during startup"
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.05)
    port: int = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", port
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()


def raw_get(port: int, target: str, host: str = "127.0.0.1") -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest("GET", target, skip_host=True, skip_accept_encoding=True)
    conn.putheader("Host", host)
    conn.endheaders()
    status = conn.getresponse().status
    conn.close()
    return status


def test_service_over_real_http(tmp_path: Path) -> None:
    called = threading.Event()
    seen: list[list[str]] = []

    async def poll(slugs: Sequence[str]) -> None:
        seen.append(list(slugs))
        called.set()

    app = create_app(make_settings(tmp_path), poll=poll)
    with serving(app) as (base, port):
        health = httpx.get(f"{base}/v1/health", params={"deep": "true"})
        assert health.status_code == 200
        assert health.json()["scheduler"] == "running"

        assert httpx.get(f"{base}/v1/status").status_code == 401
        status = httpx.get(f"{base}/v1/status", headers=AUTH).json()
        assert status["scheduler"]["state"] == "running"
        assert [job["id"] for job in status["scheduler"]["jobs"]] == [f"poll:{SLUG}"]
        assert status["scheduler"]["jobs"][0]["next_run"] is not None

        lesson = httpx.get(f"{base}/files/site/lesson.html")
        assert lesson.status_code == 200
        assert "sandbox allow-scripts" in lesson.headers["content-security-policy"]

        assert raw_get(port, "/files/../secret.html") == 404
        assert raw_get(port, "/files/%2e%2e/secret.html") == 404
        assert raw_get(port, "/files/site/..%2f..%2fsecret.html") == 404
        assert raw_get(port, "/v1/health", host="evil.example") == 400

        app.state.scheduler.modify_job(f"poll:{SLUG}", next_run_time=datetime.now(UTC))
        assert called.wait(timeout=10), "the scheduled poll did not run"
    assert seen[0] == [SLUG]


def test_lifespan_without_poll_reports_scheduler_disabled(tmp_path: Path) -> None:
    with serving(create_app(make_settings(tmp_path))) as (base, _):
        assert httpx.get(f"{base}/v1/health").json()["scheduler"] == "disabled"
