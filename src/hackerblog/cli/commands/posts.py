"""`hackerblog posts`: list and show discovered posts (section 15.3)."""

from datetime import date
from typing import Annotated, Literal, get_args

import typer

from hackerblog.cli.output import emit_json, say, table
from hackerblog.cli.state import fail, get_state
from hackerblog.core.discovery import service
from hackerblog.core.errors import HackerBlogError
from hackerblog.core.store.posts import PostQuery, PostRow

app = typer.Typer(no_args_is_help=True, help="Discovered and processed posts.")
JsonFlag = Annotated[bool, typer.Option("--json", help="Print one JSON document.")]
Status = Literal[
    "any",
    "discovered",
    "queued",
    "fetching",
    "extracted",
    "generating",
    "rendering",
    "ready",
    "skipped",
    "failed",
    "dead",
]
Reading = Literal["unread", "read", "learned", "archived"]
NOT_ARCHIVED = ("unread", "read", "learned")
NOT_SKIPPED = tuple(s for s in get_args(Status) if s not in ("any", "skipped"))


def post_json(post: PostRow) -> dict[str, object]:
    """The JSON shape of one post."""
    return {
        "id": post.id,
        "site": post.site,
        "title": post.title,
        "url": post.url,
        "canonical_url": post.canonical_url,
        "author": post.author,
        "published_at": post.published_at,
        "discovered_at": post.discovered_at,
        "origin": post.origin,
        "status": post.status,
        "skip_reason": post.skip_reason,
        "reading_status": post.reading_status,
        "last_error": post.last_error,
        "processed_at": post.processed_at,
    }


@app.command("list")
def list_posts(
    ctx: typer.Context,
    site: Annotated[list[str] | None, typer.Option("--site", help="Repeatable.")] = None,
    status: Annotated[
        Status | None, typer.Option("--status", help="Default: every status but skipped.")
    ] = None,
    unread: Annotated[bool, typer.Option("--unread")] = False,
    reading: Annotated[Reading | None, typer.Option("--reading")] = None,
    since: Annotated[str | None, typer.Option("--since", help="YYYY-MM-DD.")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=500)] = 20,
    as_json: JsonFlag = False,
) -> None:
    """List posts, newest first; every status but skipped, not archived, by default."""
    if since is not None:
        try:
            date.fromisoformat(since)
        except ValueError:
            raise typer.BadParameter("use YYYY-MM-DD", param_hint="--since") from None
    readings = ("unread",) if unread else (reading,) if reading else NOT_ARCHIVED
    query = PostQuery(
        sites=tuple(site or ()),
        statuses=NOT_SKIPPED if status is None else None if status == "any" else (status,),
        readings=readings,
        since=since,
        limit=limit,
    )
    try:
        found = service.list_posts(get_state(ctx).paths, query)
    except HackerBlogError as error:
        fail(error, as_json)
    if as_json:
        emit_json([post_json(post) for post in found])
        return
    rows = [
        [
            str(post.id),
            (post.published_at or "")[:10] or "-",
            post.site,
            (post.title or post.url)[:70],
            post.status,
            post.reading_status,
        ]
        for post in found
    ]
    table(["ID", "DATE", "SITE", "TITLE", "STATUS", "READING"], rows)


@app.command("show")
def show_post(ctx: typer.Context, post_id: int, as_json: JsonFlag = False) -> None:
    """Show one post's source, dates and pipeline status."""
    try:
        post = service.get_post(get_state(ctx).paths, post_id)
    except HackerBlogError as error:
        fail(error, as_json)
    data = post_json(post)
    if as_json:
        emit_json(data)
        return
    for key, value in data.items():
        say(f"{key}: {'-' if value is None else value}")
