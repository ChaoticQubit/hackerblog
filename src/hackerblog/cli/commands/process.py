"""Processing commands: work, process, backfill, regenerate and render (section 15.3)."""

import asyncio
import importlib
import sqlite3
import webbrowser
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Annotated, Literal, cast

import typer

from hackerblog.cli.output import emit_error, emit_json, say
from hackerblog.core import pipeline, work
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


def work_code(report: pipeline.WorkReport) -> ExitCode:
    """OK when no job failed, PARTIAL when some did, FAILED when all did."""
    states = Counter(result.state for result in report.results)
    failed = sum(states[state] for state in FAILED_STATES)
    if failed == 0:
        return ExitCode.OK
    return ExitCode.PARTIAL if failed < len(report.results) else ExitCode.FAILED


def process_queue(
    paths: Paths,
    as_json: bool,
    *,
    max_jobs: int | None = None,
    concurrency: int = 2,
    forever: bool = False,
) -> pipeline.WorkReport:
    """Run due jobs with the real fetcher, printing a line per job unless as_json.

    Raises HackerBlogError when the config, database or LLM settings are not usable.
    """

    def progress(result: pipeline.JobResult) -> None:
        if not as_json:
            detail = f" ({result.detail})" if result.detail and result.state != "ready" else ""
            say(f"job {result.job_id}  post {result.post_id}  {result.state}{detail}")

    return asyncio.run(
        work.run_queue(
            paths,
            load_backend(),
            max_jobs=max_jobs,
            concurrency=concurrency,
            forever=forever,
            on_result=progress,
        )
    )


def work_json(report: pipeline.WorkReport) -> dict[str, object]:
    """The JSON fields every processing command reports: jobs, processed and tokens."""
    jobs_json = [_job_json(result) for result in report.results]
    return {"jobs": jobs_json, "processed": len(jobs_json), "tokens": report.tokens}


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
        report = process_queue(
            paths, as_json, max_jobs=max_jobs, concurrency=concurrency, forever=forever
        )
    except HackerBlogError as error:
        _fail(error, as_json)
        return
    if report.stop is not None:
        _fail(report.stop, as_json, work_json(report))
    if as_json:
        emit_json(work_json(report))
    elif not report.results:
        say("No jobs are due.")
    else:
        states = Counter(result.state for result in report.results)
        counts = ", ".join(f"{count} {state}" for state, count in sorted(states.items()))
        say(f"{len(report.results)} jobs: {counts}; {_tokens(report.tokens)}")
    raise typer.Exit(int(work_code(report)))


def process_command(
    ctx: typer.Context,
    url: Annotated[str, typer.Argument(metavar="URL")],
    site: Annotated[str | None, typer.Option("--site", metavar="SLUG")] = None,
    force: Annotated[bool, typer.Option("--force", help="Bypass the LLM cache.")] = False,
    open_lesson: Annotated[bool, typer.Option("--open", help="Open the lesson.")] = False,
    no_wait: Annotated[bool, typer.Option("--no-wait", help="Queue only.")] = False,
    as_json: JsonFlag = False,
) -> None:
    """Process one post URL now, matched to a site by host or filed under adhoc."""
    paths = _paths(ctx)
    try:
        done = asyncio.run(
            work.process_url(paths, load_backend(), url, site=site, force=force, wait=not no_wait)
        )
        conn = _open_db(paths)
    except HackerBlogError as error:
        _fail(error, as_json)
        return
    try:
        lesson = _lesson_path(conn, done.post_id)
    finally:
        conn.close()
    status = "queued" if done.outcome is None else done.outcome.status
    tokens = 0 if done.outcome is None else done.outcome.tokens
    if as_json:
        data = {"post_id": done.post_id, "site": done.site, "status": status}
        emit_json(data | {"tokens": tokens, "lesson": lesson})
    else:
        say(f"Post {done.post_id} {status} ({_tokens(tokens)}): {lesson or ''}")
    if open_lesson and lesson is not None:
        webbrowser.open(Path(lesson).as_uri())


def _amount(tokens: int) -> str:
    assert tokens >= 0
    return f"{tokens / 1_000_000:.1f}M" if tokens >= 1_000_000 else f"{tokens // 1000}k"


def backfill_command(
    ctx: typer.Context,
    slug: Annotated[str, typer.Argument(metavar="SLUG")],
    limit: Annotated[int | None, typer.Option("--limit", min=1)] = None,
    since: Annotated[str | None, typer.Option("--since", help="YYYY-MM-DD.")] = None,
    no_process: Annotated[bool, typer.Option("--no-process", help="Queue only.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Write nothing.")] = False,
    wait: Annotated[bool, typer.Option("--wait", help="Wait for a running poll.")] = False,
    as_json: JsonFlag = False,
) -> None:
    """Discover a site, then queue its older posts up to the limit and process them."""
    if since is not None:
        try:
            date.fromisoformat(since)
        except ValueError:
            raise typer.BadParameter("use YYYY-MM-DD", param_hint="--since") from None
    paths = _paths(ctx)
    try:
        plan = asyncio.run(
            work.backfill(paths, slug, limit=limit, since=since, dry_run=dry_run, wait=wait)
        )
    except HackerBlogError as error:
        _fail(error, as_json)
        return
    if not as_json:
        verb = "Would queue" if dry_run else "Queuing"
        say(f"{verb} {len(plan.queued)} posts, estimated {_amount(plan.estimate_tokens)} tokens.")
    report = pipeline.WorkReport([])
    if not (dry_run or no_process):
        try:
            report = process_queue(paths, as_json)
        except HackerBlogError as error:
            _fail(error, as_json)
            return
    data = {"queued": plan.queued, "estimate_tokens": plan.estimate_tokens} | work_json(report)
    if report.stop is not None:
        _fail(report.stop, as_json, data)
    if as_json:
        emit_json(data)
    elif report.results:
        say(f"{len(report.results)} processed; {_tokens(report.tokens)}")
    code = plan.discovery.code if plan.discovery.code != ExitCode.OK else work_code(report)
    raise typer.Exit(int(code))


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
    """Add work, process, backfill, regenerate and render to the root app."""
    app.command("work")(work_command)
    app.command("process")(process_command)
    app.command("backfill")(backfill_command)
    app.command("regenerate")(regenerate_command)
    app.command("render")(render_command)
