"""`hackerblog sites`: list, show, add, edit and import-opml (section 15.3)."""

import asyncio
from pathlib import Path
from typing import Annotated, Literal

import typer

from hackerblog.cli.output import emit_json, say, table
from hackerblog.cli.state import fail, get_state
from hackerblog.core.discovery import service
from hackerblog.core.discovery.discover import SiteResult
from hackerblog.core.discovery.resolve import Resolution
from hackerblog.core.errors import ExitCode, HackerBlogError

app = typer.Typer(no_args_is_help=True, help="Followed blogs.")
JsonFlag = Annotated[bool, typer.Option("--json", help="Print one JSON document.")]
NotifyMode = Literal["instant", "digest", "off"]
ScheduleOption = Annotated[
    str | None,
    typer.Option("--schedule", help="hourly, daily, weekly, monthly or a 5-field cron."),
]
LimitOption = Annotated[
    int | None, typer.Option("--limit", min=1, max=200, help="Most posts taken per poll.")
]
BackfillOption = Annotated[int | None, typer.Option("--backfill-limit", min=0)]


def _summary_json(summary: service.SiteSummary) -> dict[str, object]:
    row = summary.row
    return {
        "slug": row.slug,
        "name": row.name,
        "url": row.url,
        "status": row.status,
        "method": row.discovery_method,
        "feed_url": row.feed_url,
        "sitemap_url": row.sitemap_url,
        "schedule": summary.schedule,
        "notify": summary.notify,
        "posts": summary.counts,
        "last_polled_at": row.last_polled_at,
        "last_success_at": row.last_success_at,
        "consecutive_failures": row.consecutive_failures,
        "last_error": row.last_error,
        "health": summary.health,
    }


@app.command("list")
def list_sites(
    ctx: typer.Context,
    include_all: Annotated[
        bool, typer.Option("--all", help="Include disabled and removed.")
    ] = False,
    as_json: JsonFlag = False,
) -> None:
    """List followed sites with their discovery method and health."""
    try:
        found = service.site_summaries(get_state(ctx).paths, include_all)
    except HackerBlogError as error:
        fail(error, as_json)
    if as_json:
        emit_json([_summary_json(summary) for summary in found])
        return
    rows = [
        [
            summary.row.slug,
            summary.row.name,
            summary.row.discovery_method or "-",
            summary.schedule or "-",
            summary.notify or "-",
            f"{summary.counts.get('ready', 0)}/{sum(summary.counts.values())}",
            summary.row.last_success_at or "never",
            summary.health if summary.row.status == "active" else summary.row.status,
        ]
        for summary in found
    ]
    headers = ["SLUG", "NAME", "METHOD", "SCHEDULE", "NOTIFY", "READY/TOTAL", "LAST OK", "HEALTH"]
    table(headers, rows)


@app.command("show")
def show_site(ctx: typer.Context, slug: str, as_json: JsonFlag = False) -> None:
    """Show one site's settings, sources, cache state and post counts."""
    try:
        detail = service.site_detail(get_state(ctx).paths, slug)
    except HackerBlogError as error:
        fail(error, as_json)
    limits = detail.limits
    data = {
        **_summary_json(detail.summary),
        "backfill_limit": None if limits is None else limits.backfill_limit,
        "max_new_per_poll": None if limits is None else limits.max_new_per_poll,
        "limit": None if detail.summary.config is None else detail.summary.config.limit,
        "aggregator": detail.summary.config is not None and detail.summary.config.aggregator,
        "post_url_pattern": detail.summary.row.post_url_pattern,
        "robots": None if detail.robots is None else {"policy": detail.robots[0]},
        "http_cache": [
            {"url": url, "etag": etag, "last_modified": modified, "last_status": status}
            for url, etag, modified, status, _ in detail.validators
        ],
    }
    if as_json:
        emit_json(data)
        return
    for key, value in data.items():
        if key not in {"posts", "http_cache"}:
            say(f"{key}: {'-' if value is None else value}")
    counts = ", ".join(f"{name} {count}" for name, count in detail.summary.counts.items())
    say(f"posts: {counts or 'none'}")
    for url, etag, modified, status, fetched in detail.validators:
        validators = f"etag {etag or '-'} modified {modified or '-'}"
        say(f"cache: {url} status {status} {validators} at {fetched}")


def _resolution_line(found: Resolution) -> str:
    if found.method == "feed":
        return f"Found feed: {found.feed_url}"
    if found.method == "sitemap":
        return f"No feed. Using sitemap: {found.sitemap_url}"
    return "No feed or sitemap; using link diff, consider post_url_pattern"


@app.command("add")
def add_site(
    ctx: typer.Context,
    url: str,
    name: Annotated[str | None, typer.Option("--name")] = None,
    slug: Annotated[str | None, typer.Option("--slug")] = None,
    tag: Annotated[list[str] | None, typer.Option("--tag", help="Repeatable.")] = None,
    feed: Annotated[str | None, typer.Option("--feed", help="Feed override.")] = None,
    notify: Annotated[NotifyMode | None, typer.Option("--notify")] = None,
    backfill_limit: BackfillOption = None,
    schedule: ScheduleOption = None,
    limit: LimitOption = None,
    aggregator: Annotated[
        bool, typer.Option("--aggregator", help="Allow feed entries on other hosts.")
    ] = False,
    no_discover: Annotated[bool, typer.Option("--no-discover")] = False,
    poll: Annotated[bool, typer.Option("--poll", help="Poll the site right after.")] = False,
    as_json: JsonFlag = False,
) -> None:
    """Validate a blog URL, find its feed or sitemap, and append it to sites.toml."""
    state = get_state(ctx)
    spec = service.NewSite(
        url, name, slug, tuple(tag or ()), feed, notify, backfill_limit, schedule, limit, aggregator
    )
    try:
        added = asyncio.run(service.add_site(state.paths, spec, discover=not no_discover))
        report = None
        if poll:
            assert added.site.slug is not None
            report = asyncio.run(service.poll(state.paths, [added.site.slug]))
    except HackerBlogError as error:
        fail(error, as_json)
    site_slug = added.site.slug
    assert site_slug is not None
    found = added.resolution
    if as_json:
        data: dict[str, object] = {
            "slug": site_slug,
            "url": added.site.url,
            "name": added.site.name,
            "method": None if found is None else found.method,
            "feed_url": None if found is None else found.feed_url,
            "sitemap_url": None if found is None else found.sitemap_url,
        }
        if report is not None:
            data["poll"] = {"new_posts": report.new_posts, "code": report.code.slug}
        emit_json(data)
    else:
        if found is not None:
            say(_resolution_line(found))
        say(f"Added site {site_slug} to {state.paths.config}")
        if report is not None:
            for result in report.results:
                say(site_line(result))
        elif not state.quiet:
            say(f"Next: hackerblog poll --site {site_slug}")
    if report is not None and report.code != ExitCode.OK:
        raise typer.Exit(int(report.code))


@app.command("edit")
def edit_site(
    ctx: typer.Context,
    slug: str,
    schedule: ScheduleOption = None,
    limit: LimitOption = None,
    no_limit: Annotated[bool, typer.Option("--no-limit", help="Remove the limit.")] = False,
    backfill_limit: BackfillOption = None,
    notify: Annotated[NotifyMode | None, typer.Option("--notify")] = None,
    aggregator: Annotated[
        bool | None, typer.Option("--aggregator/--no-aggregator", show_default=False)
    ] = None,
    enabled: Annotated[bool | None, typer.Option("--enable/--disable", show_default=False)] = None,
    as_json: JsonFlag = False,
) -> None:
    """Change one site's settings in sites.toml, keeping the rest of the file as is."""
    state = get_state(ctx)
    if no_limit and limit is not None:
        fail(HackerBlogError(ExitCode.USAGE, "pass --limit or --no-limit, not both", ""), as_json)
    changes: dict[str, object | None] = {
        key: value
        for key, value in {
            "schedule": schedule,
            "limit": limit,
            "backfill_limit": backfill_limit,
            "notify": notify,
            "enabled": enabled,
        }.items()
        if value is not None
    }
    if no_limit:
        changes["limit"] = None
    if aggregator is not None:
        changes["aggregator"] = True if aggregator else None
    if not changes:
        hint = "Pass at least one option, see: hackerblog sites edit --help"
        fail(HackerBlogError(ExitCode.USAGE, "nothing to change", hint), as_json)
    try:
        site = service.edit_site(state.paths, slug, changes)
    except HackerBlogError as error:
        fail(error, as_json)
    if as_json:
        emit_json({"slug": slug, "changed": sorted(changes), "site": site.model_dump(mode="json")})
        return
    say(f"Updated site {slug} in {state.paths.config}: {', '.join(sorted(changes))}")


def site_line(result: SiteResult) -> str:
    """One `poll` output line for a site: slug, method and what happened."""
    method = result.method or "-"
    if result.error is not None:
        outcome = f"failed: {result.error}"
    elif result.first_sync:
        outcome = (
            f"first sync: {result.found} posts found, {result.queued} queued (backfill), "
            f"{result.skipped} skipped"
        )
    elif result.not_modified:
        outcome = "304 not modified"
    else:
        outcome = f"+{result.new} new, {result.queued} queued"
    return f"{result.slug}  {method}  {outcome}"


@app.command("import-opml")
def import_opml(
    ctx: typer.Context,
    file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be added.")] = False,
    tag: Annotated[list[str] | None, typer.Option("--tag", help="Repeatable.")] = None,
    as_json: JsonFlag = False,
) -> None:
    """Add one site per OPML outline that is not followed yet."""
    state = get_state(ctx)
    try:
        result = service.import_opml(state.paths, file, tuple(tag or ()), dry_run)
    except HackerBlogError as error:
        fail(error, as_json)
    added = [{"slug": site.slug, "url": site.url, "feed": site.feed} for site in result.added]
    if as_json:
        data = {"added": added, "existing": result.existing, "invalid": result.invalid}
        emit_json(data, [f"invalid outline {item}" for item in result.invalid])
    else:
        verb = "Would add" if dry_run else "Added"
        for item in added:
            say(f"{verb} {item['slug']}  {item['url']}")
        for url in result.existing:
            say(f"Already followed: {url}")
        for item in result.invalid:
            say(f"Invalid outline: {item}")
        counts = (len(added), len(result.existing), len(result.invalid))
        say("{} added, {} already followed, {} invalid".format(*counts))
    if result.invalid:
        raise typer.Exit(int(ExitCode.PARTIAL))
