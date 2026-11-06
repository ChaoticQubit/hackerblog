"""The `hackerblog` command line, a thin Typer adapter over hackerblog.core.service."""

import asyncio
import os
import signal
import sys
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

import typer

from hackerblog import __version__
from hackerblog.cli.commands import posts as posts_commands
from hackerblog.cli.commands import sites as sites_commands
from hackerblog.cli.commands.sites import site_line
from hackerblog.cli.output import clean, emit_json, say
from hackerblog.cli.state import State, fail, get_state
from hackerblog.core import service
from hackerblog.core.discovery import service as discovery
from hackerblog.core.errors import ExitCode, HackerBlogError
from hackerblog.core.logging import setup_logging
from hackerblog.core.paths import resolve_paths

app = typer.Typer(
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
    add_completion=False,
    help="Follow blogs and turn every post into notes and an interactive lesson.",
)

Provider = Literal["anthropic", "openai", "openai_compatible", "ollama"]
JsonFlag = Annotated[bool, typer.Option("--json", help="Print one JSON document.")]


def _show_version(value: bool) -> None:
    if value:
        say(f"hackerblog {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[Path | None, typer.Option("--config", help="Config file.")] = None,
    home: Annotated[Path | None, typer.Option("--home", help="Data folder.")] = None,
    verbose: Annotated[int, typer.Option("-v", "--verbose", count=True)] = 0,
    quiet: Annotated[bool, typer.Option("-q", "--quiet")] = False,
    no_color: Annotated[bool, typer.Option("--no-color")] = False,
    version: Annotated[
        bool, typer.Option("--version", callback=_show_version, is_eager=True)
    ] = False,
) -> None:
    """Global options. Paths also come from HACKERBLOG_HOME and HACKERBLOG_CONFIG."""
    if no_color:
        os.environ["NO_COLOR"] = "1"
    setup_logging(verbose)
    ctx.obj = State(resolve_paths(home, config), quiet)


@app.command()
def init(
    ctx: typer.Context,
    output_dir: Annotated[Path | None, typer.Option("--output-dir")] = None,
    provider: Annotated[Provider, typer.Option("--provider")] = "anthropic",
    force: Annotated[bool, typer.Option("--force", help="Overwrite sites.toml.")] = False,
    yes: Annotated[bool, typer.Option("-y", "--yes")] = False,
    as_json: JsonFlag = False,
) -> None:
    """Create the home folder, a starter sites.toml, secrets.env and the database."""
    state = get_state(ctx)
    if force and state.paths.config.exists() and not yes:
        if not sys.stdin.isatty():
            usage = HackerBlogError(
                ExitCode.USAGE, "--force would overwrite sites.toml", "Add --yes to confirm"
            )
            fail(usage, as_json)
        typer.confirm(f"Overwrite {state.paths.config}?", abort=True)
    try:
        result = service.init_home(state.paths, output_dir, provider, force)
    except HackerBlogError as error:
        fail(error, as_json)
        return
    paths = result.paths
    if as_json:
        emit_json(
            {
                "home": str(paths.home),
                "config": str(paths.config),
                "secrets": str(paths.secrets),
                "database": str(paths.db),
                "output_dir": str(result.output_dir) if result.output_dir else None,
                "wrote_config": result.wrote_config,
                "wrote_secrets": result.wrote_secrets,
            }
        )
        return
    say(f"Home: {paths.home}")
    say(f"Config: {paths.config} ({'written' if result.wrote_config else 'kept'})")
    say(f"Secrets: {paths.secrets} ({'written' if result.wrote_secrets else 'kept'})")
    say(f"Database: {paths.db}")
    if result.output_dir:
        say(f"Output folder: {result.output_dir}")
    if state.quiet:
        return
    if result.key_name:
        say(f"Next: add your key to {paths.secrets} ({result.key_name}=...),")
    else:
        say(f"Next: set notes_model and lesson_model in [llm] of {paths.config},")
    say("then run: hackerblog check")


@app.command()
def check(
    ctx: typer.Context,
    online: Annotated[bool, typer.Option("--online", help="Also resolve every site.")] = False,
    as_json: JsonFlag = False,
) -> None:
    """Validate sites.toml and secrets; --online also resolves sites, writing nothing."""
    state = get_state(ctx)
    report = service.check(state.paths, os.environ)
    name = state.paths.config.name
    if report.code == ExitCode.CONFIG:
        count = len(report.problems)
        summary = f"{count} problem{'' if count == 1 else 's'}"
        if as_json:
            problems: list[object] = [
                {"location": p.location, "line": p.line, "message": p.message, "hint": p.hint}
                for p in report.problems
            ]
            error = HackerBlogError(ExitCode.CONFIG, summary, f"Edit {state.paths.config}")
            fail(error, as_json, {"problems": problems})
        for problem in report.problems:
            say(problem.render(name))
        say(summary)
        raise typer.Exit(int(ExitCode.CONFIG))
    resolved = asyncio.run(discovery.check_online(state.paths)) if online else []
    if not as_json:
        for item in resolved:
            say(_online_line(item))
    if report.missing_key:
        fail(
            HackerBlogError(
                ExitCode.LLM_AUTH,
                f"{report.missing_key} is not set",
                f"Add {report.missing_key}=... to {state.paths.secrets} or the environment",
            ),
            as_json,
        )
    config = report.config
    assert config is not None
    models = f"{config.llm.notes_model}, {config.llm.lesson_model}"
    unreachable = [item for item in resolved if item.error]
    if as_json:
        data: dict[str, object] = {"sites": len(config.sites), "provider": config.llm.provider}
        if online:
            data["online"] = [_online_json(item) for item in resolved]
        emit_json(data, [f"{item.slug}: {item.error}" for item in unreachable])
    elif unreachable:
        say(f"{len(unreachable)} of {len(resolved)} sites unreachable")
    else:
        say(f"OK: {len(config.sites)} sites, provider {config.llm.provider} ({models})")
    if unreachable:
        raise typer.Exit(int(ExitCode.PARTIAL))


def _online_line(item: discovery.OnlineResult) -> str:
    if item.resolution is None:
        return f"{item.slug}  unreachable: {item.error}"
    found = item.resolution
    source = found.feed_url or found.sitemap_url or "index page"
    return f"{item.slug}  {found.method}  {source}"


def _online_json(item: discovery.OnlineResult) -> dict[str, object]:
    found = item.resolution
    return {
        "slug": item.slug,
        "method": None if found is None else found.method,
        "feed_url": None if found is None else found.feed_url,
        "sitemap_url": None if found is None else found.sitemap_url,
        "error": item.error,
    }


@app.command()
def poll(
    ctx: typer.Context,
    site: Annotated[list[str] | None, typer.Option("--site", help="Repeatable.")] = None,
    no_process: Annotated[
        bool, typer.Option("--no-process", help="Discover and queue only.")
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Write nothing.")] = False,
    wait: Annotated[bool, typer.Option("--wait", help="Wait for a running poll.")] = False,
    as_json: JsonFlag = False,
) -> None:
    """Discover new posts on every enabled site and queue them for processing."""
    state = get_state(ctx)
    try:
        report = asyncio.run(discovery.poll(state.paths, site or (), dry_run=dry_run, wait=wait))
    except HackerBlogError as error:
        fail(error, as_json)
    failed = [result for result in report.results if result.error]
    if as_json:
        data = {
            "poll_run_id": report.poll_run_id,
            "dry_run": report.dry_run,
            "sites_ok": len(report.results) - len(failed),
            "sites_failed": len(failed),
            "new_posts": report.new_posts,
            "processed": 0,
            "sites": [asdict(result) for result in report.results],
        }
        emit_json(data, [f"{result.slug}: {result.error}" for result in failed])
    else:
        for result in report.results:
            say(site_line(result))
        prefix = "dry run: " if report.dry_run else ""
        count = len(report.results)
        say(f"{prefix}{count} sites polled, {report.new_posts} new, {len(failed)} failed")
    if report.code != ExitCode.OK:
        raise typer.Exit(int(report.code))


app.add_typer(sites_commands.app, name="sites")
app.add_typer(posts_commands.app, name="posts")


def _interrupted(signum: int, frame: object) -> None:
    raise SystemExit(int(ExitCode.INTERRUPTED))


def run() -> None:
    """Console entry point: maps Ctrl-C to 130 and unexpected errors to exit code 1."""
    signal.signal(signal.SIGINT, _interrupted)
    try:
        app()
    except Exception as error:
        sys.stderr.write(f"Internal error: {clean(str(error))}\n")
        if "-vv" in sys.argv:
            sys.stderr.write(clean(traceback.format_exc()))
        sys.stderr.write("Try: rerun with -vv and report the output\n")
        sys.exit(int(ExitCode.INTERNAL))
