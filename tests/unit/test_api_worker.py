import asyncio
from pathlib import Path

import httpx
from tests.unit.test_api_app import settings

from hackerblog.api.app import create_app


def test_serve_runs_the_worker_and_health_reports_it(tmp_path: Path) -> None:
    started = asyncio.Event()
    cancelled: list[bool] = []

    async def worker() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    app = create_app(settings(tmp_path), worker=worker)

    async def scenario() -> tuple[str, str]:
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(started.wait(), 5)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
                running = (await http.get("/v1/health")).json()["worker"]
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
            after = (await http.get("/v1/health")).json()["worker"]
        return running, after

    running, after = asyncio.run(scenario())
    assert running == "running"
    assert after == "disabled"
    assert cancelled == [True]


def test_health_reports_a_dead_worker_as_stopped(tmp_path: Path) -> None:
    async def worker() -> None:
        raise RuntimeError("boom")

    app = create_app(settings(tmp_path), worker=worker)

    async def scenario() -> str:
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.05)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
                return (await http.get("/v1/health")).json()["worker"]

    assert asyncio.run(scenario()) == "stopped"
