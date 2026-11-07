"""FastAPI app factory: a thin adapter over hackerblog.core (docs/architecture.md section 16)."""

import asyncio
import logging
import sqlite3
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from hackerblog import __version__
from hackerblog.api.files import MEDIA_TYPES, file_headers, resolve_output_file
from hackerblog.api.scheduler import PollFn, build_scheduler
from hackerblog.api.security import (
    LOOPBACK_HOSTS,
    STATUS_CODES,
    BodyLimit,
    SecurityHeaders,
    TrustedHost,
    is_loopback,
    problem,
    require_token,
)
from hackerblog.core.config import Config
from hackerblog.core.paths import Paths

logger = logging.getLogger("hackerblog.api")
Authorized = Annotated[None, Depends(require_token)]
WORKER_DRAIN_S = 30.0


@dataclass(frozen=True)
class ServiceSettings:
    """What the service needs: the API token, paths, validated config and bind address."""

    token: str
    paths: Paths
    config: Config
    bind_host: str = "127.0.0.1"

    def __post_init__(self) -> None:
        assert len(self.token) >= 16, "the API token is too short"


def _scheduler_state(scheduler: Any) -> str:
    if scheduler is None:
        return "disabled"
    return "running" if scheduler.running else "stopped"


def _worker_state(worker: asyncio.Task[None] | None) -> str:
    if worker is None:
        return "disabled"
    return "stopped" if worker.done() else "running"


async def _stop_worker(worker: asyncio.Task[None]) -> None:
    worker.cancel()
    done, _ = await asyncio.wait({worker}, timeout=WORKER_DRAIN_S)
    if worker in done and not worker.cancelled() and worker.exception() is not None:
        logger.error("worker.crashed", exc_info=worker.exception())


def _database_ok(settings: ServiceSettings, deep: bool) -> bool:
    try:
        conn = sqlite3.connect(f"{settings.paths.db.as_uri()}?mode=rw", uri=True, timeout=2)
    except sqlite3.Error:
        return False
    try:
        conn.execute("SELECT count(*) FROM meta").fetchone()
        if deep:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    return True


def _add_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
        code = STATUS_CODES.get(error.status_code, "http_error")
        return problem(error.status_code, code, str(error.detail), dict(error.headers or {}))

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, error: RequestValidationError) -> JSONResponse:
        return problem(422, "validation", "the request is not valid")

    @app.exception_handler(Exception)
    async def internal(request: Request, error: Exception) -> JSONResponse:
        logger.exception("api.internal_error")
        return problem(500, "internal", "unexpected error")


def _add_routes(app: FastAPI, settings: ServiceSettings) -> None:
    loopback = is_loopback(settings.bind_host)

    @app.get("/v1/health")
    def health(request: Request, deep: bool = False) -> JSONResponse:
        db_ok = _database_ok(settings, deep)
        body = {
            "status": "ok" if db_ok else "error",
            "version": __version__,
            "db": "ok" if db_ok else "error",
            "scheduler": _scheduler_state(request.app.state.scheduler),
            "worker": _worker_state(request.app.state.worker),
        }
        return JSONResponse(body, 200 if db_ok else 503)

    @app.get("/v1/status")
    def status(request: Request, _: Authorized) -> dict[str, object]:
        scheduler = request.app.state.scheduler
        jobs: list[dict[str, str | None]] = []
        if scheduler is not None:
            for job in scheduler.get_jobs():
                run = job.next_run_time
                jobs.append({"id": job.id, "next_run": run.isoformat() if run else None})
        return {
            "version": __version__,
            "config": str(settings.paths.config),
            "database": str(settings.paths.db),
            "sites": len(settings.config.sites),
            "scheduler": {"state": _scheduler_state(scheduler), "jobs": jobs},
        }

    @app.get("/v1/openapi.json", include_in_schema=False)
    def openapi(request: Request, _: Authorized) -> dict[str, Any]:
        schema: dict[str, Any] = request.app.openapi()
        return schema

    @app.get("/files/{path:path}")
    def files(request: Request, path: str) -> FileResponse:
        if not loopback:
            require_token(request)
        found = resolve_output_file(settings.config.output.dir, path)
        if found is None:
            raise HTTPException(404, "no such file")
        return FileResponse(
            found, media_type=MEDIA_TYPES[found.suffix], headers=file_headers(found)
        )


def create_app(
    settings: ServiceSettings,
    poll: PollFn | None = None,
    worker: Callable[[], Coroutine[Any, Any, None]] | None = None,
) -> FastAPI:
    """Build the ASGI app: routes, problem handlers, Trusted Host and body limit.

    With poll, the lifespan starts an APScheduler job per enabled site that calls it, and
    with worker it runs worker() as a task, cancelled on shutdown; each absent one is
    reported as disabled by /v1/health.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        scheduler = None if poll is None else build_scheduler(settings.config, poll)
        if scheduler is not None:
            scheduler.start()
        app.state.scheduler = scheduler
        task = None if worker is None else asyncio.create_task(worker())
        app.state.worker = task
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.shutdown(wait=False)
            app.state.scheduler = None
            if task is not None:
                await _stop_worker(task)
            app.state.worker = None

    api = FastAPI(
        title="HackerBlog",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    api.state.settings = settings
    api.state.scheduler = None
    api.state.worker = None
    _add_handlers(api)
    _add_routes(api, settings)
    extra = (
        set[str]() if is_loopback(settings.bind_host) else {settings.bind_host.lower().strip("[]")}
    )
    allowed = LOOPBACK_HOSTS | extra
    api.add_middleware(BodyLimit)
    api.add_middleware(TrustedHost, allowed=frozenset(allowed))
    api.add_middleware(SecurityHeaders)
    return api
