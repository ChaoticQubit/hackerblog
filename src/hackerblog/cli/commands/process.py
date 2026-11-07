"""Processing commands: work, regenerate and render (docs/architecture.md section 15.3)."""

import asyncio
import importlib
import sqlite3
from collections import Counter
from typing import Annotated, Literal, cast

import typer

from hackerblog.cli.output import emit_error, emit_json, say
from hackerblog.core import pipeline
from hackerblog.core.errors import ExitCode, HackerBlogError
from hackerblog.core.paths import Paths
from hackerblog.core.store import repo
from hackerblog.core.store.migrate import connect, migrate

JsonFlag = Annotated[bool, typer.Option("--json", help="Print one JSON document.")]
FAILED_STATES = frozenset({"failed", "dead"})


def _paths(ctx: typer.Context) -> Paths:
    paths = getattr(ctx.find_root().obj, "paths", None)
    assert isinstance(paths, Paths)
    return paths


def _fail(error: HackerBlogError, as_json: bool, extra: dict[str, object] | None = None) -> None:
    emit_error(error, as_json, extra)
    raise typer.Exit(int(error.code))


def _open_db(paths: Paths) -> sqlite3.Connection:
    if not paths.db.exists():
        raise HackerBlogError(ExitCode.CONFIG, f"No database at {paths.db}", "hackerblog init")
    conn = connect(paths.db)
    migrate(conn)
    return conn


def load_backend() -> pipeline.Backend:
    """Import the Agno backend only when a command calls the LLM; it is slow to import."""
    return cast(pipeline.Backend, importlib.import_module("hackerblog.core.llm.agno_backend"))


def _tokens(count: int) -> str:
    return f"{count / 1000:.1f}k tokens"


def _lesson_path(conn: sqlite3.Connection, post_id: int) -> str | None:
    found = [a.path for a in repo.artifacts_for(conn, post_id) if a.kind == "lesson"]
    return found[0] if found else None


def _job_json(result: pipeline.JobResult) -> dict[str, object]:
    return {
        "job_id": result.job_id,
        "post_id": result.post_id,
        "state": result.state,
        "tokens": result.tokens,
        "detail": result.detail,
    }


def _work_code(report: pipeline.WorkReport) -> ExitCode:
    states = Counter(result.state for result in report.results)
    failed = sum(states[state] for state in FAILED_STATES)
    if failed == 0:
        return ExitCode.OK
    return ExitCode.PARTIAL if failed < len(report.results) else ExitCode.FAILED


def work_command(
    ctx: typer.Context,
    max_jobs: Annotated[int | None, typer.Option("--max-jobs", min=1)] = None,
    forever: Annotated[bool, typer.Option("--forever")] = False,
    concurrency: Annotated[
        int, typer.Option("--concurrency", min=1, max=pipeline.CONCURRENCY_MAX)
    ] = 2,
    as_json: JsonFlag = False,
) -> None:
    """Run the job worker without discovery until the queue is empty."""
    paths = _paths(ctx)
    try:
        conn = _open_db(paths)
        settings = pipeline.load_settings(paths, need_llm=True)
        backend = load_backend()
    except HackerBlogError as error:
        _fail(error, as_json)
        return

    def progress(result: pipeline.JobResult) -> None:
        if not as_json:
            detail = f" ({result.detail})" if result.detail and result.state != "ready" else ""
            say(f"job {result.job_id}  post {result.post_id}  {result.state}{detail}")

    report = asyncio.run(
        pipeline.run_worker(
            conn,
            settings,
            backend,
            fetcher=None,
            max_jobs=max_jobs,
            concurrency=concurrency,
            forever=forever,
            on_result=progress,
        )
    )
    jobs_json = [_job_json(result) for result in report.results]
    if report.stop is not None:
        _fail(report.stop, as_json, {"jobs": jobs_json, "tokens": report.tokens})
    if as_json:
        emit_json({"jobs": jobs_json, "tokens": report.tokens})
    elif not report.results:
        say("No jobs are due.")
    else:
        states = Counter(result.state for result in report.results)
        counts = ", ".join(f"{count} {state}" for state, count in sorted(states.items()))
        say(f"{len(report.results)} jobs: {counts}; {_tokens(report.tokens)}")
    raise typer.Exit(int(_work_code(report)))


def regenerate_command(
    ctx: typer.Context,
    post_id: Annotated[int, typer.Argument(metavar="ID")],
    stage: Annotated[
        Literal["all", "lesson", "render"],
        typer.Option("--stage", help="all reruns digest and lesson, render costs no tokens."),
    ] = "all",
    as_json: JsonFlag = False,
) -> None:
    """Rerun a post's digest and lesson, only the lesson, or only the rendering."""
    paths = _paths(ctx)
    try:
        conn = _open_db(paths)
        settings = pipeline.load_settings(paths, need_llm=stage != "render")
        backend = None if stage == "render" else load_backend()
        outcome = asyncio.run(pipeline.rerun(conn, post_id, settings, backend, stage=stage))
    except HackerBlogError as error:
        _fail(error, as_json)
        return
    lesson = _lesson_path(conn, post_id)
    if as_json:
        emit_json(
            {
                "post_id": post_id,
                "status": outcome.status,
                "tokens": outcome.tokens,
                "lesson": lesson,
            }
        )
        return
    say(f"Post {post_id} {outcome.status} ({_tokens(outcome.tokens)}): {lesson or ''}")


def render_command(
    ctx: typer.Context,
    sites: Annotated[list[str] | None, typer.Option("--site", metavar="SLUG")] = None,
    as_json: JsonFlag = False,
) -> None:
    """Re-render notes, lessons and index.html of every ready post; never calls the LLM."""
    paths = _paths(ctx)
    slugs = tuple(sites or ())
    try:
        conn = _open_db(paths)
        settings = pipeline.load_settings(paths, need_llm=False)
        known = {row[0] for row in conn.execute("SELECT slug FROM sites")}
        unknown = [slug for slug in slugs if slug not in known]
        if unknown:
            raise HackerBlogError(
                ExitCode.NOT_FOUND, f"unknown site {unknown[0]}", "hackerblog sites list"
            )
    except HackerBlogError as error:
        _fail(error, as_json)
        return
    posts = repo.ready_posts(conn, slugs)
    failed: list[dict[str, object]] = []
    for post in posts:
        try:
            asyncio.run(
                pipeline.rerun(conn, post.id, settings, None, stage="render", update_index=False)
            )
        except HackerBlogError as error:
            failed.append({"post_id": post.id, "error": error.message})
    index = pipeline.write_index(conn, settings.output_dir)
    rendered = len(posts) - len(failed)
    assert rendered >= 0
    if as_json:
        emit_json({"rendered": rendered, "failed": failed, "index": str(index)})
    else:
        for item in failed:
            say(f"post {item['post_id']}  failed: {item['error']}")
        say(f"{rendered} posts rendered, {len(failed)} failed; index: {index}")
    if failed:
        raise typer.Exit(int(ExitCode.PARTIAL if rendered else ExitCode.FAILED))


def register(app: typer.Typer) -> None:
    """Add work, regenerate and render to the root app."""
    app.command("work")(work_command)
    app.command("regenerate")(regenerate_command)
    app.command("render")(render_command)
