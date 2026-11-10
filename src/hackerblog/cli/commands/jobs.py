"""`hackerblog jobs`: rerun failed processing jobs (section 15.3)."""

import sqlite3
from datetime import UTC, datetime
from typing import Annotated

import typer

from hackerblog.cli.output import emit_json, say
from hackerblog.cli.state import fail, get_state
from hackerblog.core import jobs
from hackerblog.core.errors import ExitCode, HackerBlogError
from hackerblog.core.store.migrate import connect, migrate

app = typer.Typer(no_args_is_help=True, help="Processing jobs.")
JsonFlag = Annotated[bool, typer.Option("--json", help="Print one JSON document.")]


def _retry(conn: sqlite3.Connection, job_id: int | None, now: datetime) -> list[int]:
    if job_id is None:
        return jobs.retry_all(conn, now)
    job = jobs.get_job(conn, job_id)
    if job is None:
        hint = "Try: hackerblog posts list --status failed"
        raise HackerBlogError(ExitCode.NOT_FOUND, f"no job with id {job_id}", hint)
    if job.state not in {"failed", "dead"}:
        raise HackerBlogError(ExitCode.USAGE, f"job {job_id} is {job.state}, not failed or dead")
    if not jobs.retry(conn, job_id, now):
        raise HackerBlogError(ExitCode.USAGE, f"post of job {job_id} already has an open job")
    return [job_id]


@app.command("retry")
def retry_jobs(
    ctx: typer.Context,
    job_id: Annotated[int | None, typer.Argument(metavar="[ID]")] = None,
    as_json: JsonFlag = False,
) -> None:
    """Requeue job ID, or every failed or dead job, due now with its attempts reset."""
    paths = get_state(ctx).paths
    try:
        if not paths.db.exists():
            raise HackerBlogError(ExitCode.CONFIG, f"No database at {paths.db}", "hackerblog init")
        conn = connect(paths.db)
        try:
            migrate(conn)
            retried = _retry(conn, job_id, datetime.now(UTC))
        finally:
            conn.close()
    except HackerBlogError as error:
        fail(error, as_json)
    if as_json:
        emit_json({"retried": retried})
    elif not retried:
        say("No failed or dead jobs to retry.")
    else:
        say(f"Retried {len(retried)} job(s): {', '.join(f'job {i}' for i in retried)}")
        say("Run: hackerblog work")
