import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hackerblog.cli.main import app
from hackerblog.core.config import FetchConfig, SiteConfig
from hackerblog.core.discovery import discover
from hackerblog.core.discovery.model import Candidate
from hackerblog.core.net.fetcher import Fetcher
from hackerblog.core.store import sites
from hackerblog.core.store.migrate import connect, migrate

from .server import FixtureServer, serve_blogs

runner = CliRunner()


@pytest.fixture(scope="module")
def server() -> Iterator[FixtureServer]:
    with serve_blogs() as running:
        yield running


@pytest.fixture
def env(tmp_path: Path, server: FixtureServer) -> dict[str, str]:
    values = {
        "HOME": str(tmp_path),
        "HACKERBLOG_HOME": str(tmp_path / "hb"),
        "HACKERBLOG_CONFIG": "",
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "NO_COLOR": "1",
    }
    code, out = invoke(values, "init", "--output-dir", str(tmp_path / "out"))
    assert code == 0, out
    base = server.base_url
    config = "\n".join(
        [
            "version = 1",
            "",
            "[output]",
            f"dir = {json.dumps(str(tmp_path / 'out'))}",
            "",
            "[fetch]",
            "min_delay_seconds = 0.5",
            'allow_private_hosts = ["127.0.0.1"]',
            "",
            *(f'[[site]]\nurl = "{base}/{name}/"\nslug = "{name}"\n' for name in SITES),
        ]
    )
    (tmp_path / "hb" / "sites.toml").write_text(config, encoding="utf-8")
    server.reset_counts()
    return values


SITES = ("feedblog", "mapblog", "plainblog")


def invoke(env: dict[str, str], *args: str) -> tuple[int, str]:
    result = runner.invoke(app, list(args), env=env)
    return result.exit_code, result.output


def poll(env: dict[str, str], *args: str) -> Any:
    code, out = invoke(env, "poll", "--no-process", "--json", *args)
    assert code == 0, out
    document = json.loads(out)
    assert document["ok"] is True
    return document["data"]


def by_slug(report: Any) -> dict[str, Any]:
    results = report["sites"]
    return {item["slug"]: item for item in results}


def db(env: dict[str, str]) -> sqlite3.Connection:
    return sqlite3.connect(Path(env["HACKERBLOG_HOME"]) / "hackerblog.db")


def stored_urls(env: dict[str, str], slug: str) -> set[str]:
    with db(env) as conn:
        rows = conn.execute(
            "SELECT p.url FROM posts p JOIN sites s ON s.id = p.site_id WHERE s.slug = ?", (slug,)
        ).fetchall()
    return {row[0] for row in rows}


def test_three_site_types_are_discovered_then_all_304(
    env: dict[str, str], server: FixtureServer
) -> None:
    first = by_slug(poll(env))
    assert {slug: first[slug]["method"] for slug in SITES} == {
        "feedblog": "feed",
        "mapblog": "sitemap",
        "plainblog": "linkdiff",
    }
    for slug in SITES:
        assert first[slug]["first_sync"] is True
        assert first[slug]["error"] is None
        assert stored_urls(env, slug) == set(server.post_urls(slug)), slug
    with db(env) as conn:
        statuses = conn.execute("SELECT DISTINCT origin, status FROM posts").fetchall()
        jobs = conn.execute("SELECT COUNT(*) FROM jobs WHERE state = 'pending'").fetchone()[0]
    assert statuses == [("backfill", "queued")]
    assert jobs == sum(len(server.post_urls(slug)) for slug in SITES)

    server.reset_counts()
    second = by_slug(poll(env))
    assert all(second[slug]["not_modified"] is True for slug in SITES), second
    assert all(second[slug]["new"] == 0 for slug in SITES)
    statuses_seen = [status for _, _, status in server.requests]
    assert statuses_seen
    assert set(statuses_seen) == {304}, server.requests


def test_new_post_is_noticed_once_as_new(env: dict[str, str], server: FixtureServer) -> None:
    poll(env)
    published = {
        slug: server.publish(slug, f"fresh-post-on-{slug}", f"Fresh {slug}") for slug in SITES
    }
    third = by_slug(poll(env))
    for slug, url in published.items():
        assert third[slug]["new"] == 1, (slug, third[slug])
        assert third[slug]["queued"] == 1
        assert url in stored_urls(env, slug)
    with db(env) as conn:
        origins = conn.execute(
            "SELECT origin, status FROM posts WHERE url IN (SELECT value FROM json_each(?))",
            (json.dumps(list(published.values())),),
        ).fetchall()
    assert origins == [("new", "queued")] * 3
    fourth = by_slug(poll(env))
    assert all(fourth[slug]["new"] == 0 for slug in SITES)


def table_counts(env: dict[str, str]) -> dict[str, int]:
    with db(env) as conn:
        return {
            "posts": conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0],
            "http_cache": conn.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0],
            "robots_cache": conn.execute("SELECT COUNT(*) FROM robots_cache").fetchone()[0],
            "poll_runs": conn.execute("SELECT COUNT(*) FROM poll_runs").fetchone()[0],
        }


def test_dry_run_and_check_online_write_nothing(env: dict[str, str], server: FixtureServer) -> None:
    invoke(env, "sites", "list")
    empty = table_counts(env)
    report = poll(env, "--dry-run")
    assert report["poll_run_id"] is None
    assert report["new_posts"] == sum(len(server.post_urls(slug)) for slug in SITES)
    code, out = invoke(env, "check", "--online", "--json")
    assert code == 0, out
    online = json.loads(out)["data"]["online"]
    assert [item["method"] for item in online] == ["feed", "sitemap", "linkdiff"]
    assert (
        table_counts(env)
        == empty
        == {
            "posts": 0,
            "http_cache": 0,
            "robots_cache": 0,
            "poll_runs": 0,
        }
    )


def test_sites_add_finds_each_source_kind(env: dict[str, str], server: FixtureServer) -> None:
    config = Path(env["HACKERBLOG_HOME"]) / "sites.toml"
    config.write_text(config.read_text(encoding="utf-8").split("[[site]]")[0], encoding="utf-8")
    expected = {"feedblog": "Found feed", "mapblog": "Using sitemap", "plainblog": "link diff"}
    for slug, phrase in expected.items():
        code, out = invoke(env, "sites", "add", f"{server.base_url}/{slug}/", "--slug", slug)
        assert code == 0, out
        assert phrase in out
    code, out = invoke(env, "sites", "list", "--json")
    assert [item["slug"] for item in json.loads(out)["data"]] == list(expected)


def test_poll_refuses_a_redirect_into_a_private_address(
    env: dict[str, str], server: FixtureServer
) -> None:
    config = Path(env["HACKERBLOG_HOME"]) / "sites.toml"
    feed = f"{server.base_url}/redirect-to?url=http://169.254.169.254/latest/meta-data/"
    block = f'[[site]]\nurl = "{server.base_url}/evil/"\nslug = "evil"\nfeed = "{feed}"\n'
    config.write_text(config.read_text(encoding="utf-8") + "\n" + block, encoding="utf-8")
    code, out = invoke(env, "poll", "--no-process", "--site", "evil", "--json")
    assert code == 6, out
    result = json.loads(out)["data"]["sites"][0]
    assert result["blocked"] is True
    assert "169.254.169.254" in result["error"]


@pytest.mark.anyio
async def test_screen_checks_robots_on_own_origin_and_ignores_foreign_links(
    tmp_path: Path, server: FixtureServer
) -> None:
    conn = connect(tmp_path / "hb.db")
    migrate(conn)
    site = SiteConfig(url=f"{server.base_url}/plainblog/", slug="plainblog")
    sites.sync_sites(conn, [site], "2026-11-06T08:00:00Z")
    row = sites.get_site(conn, "plainblog")
    assert row is not None
    own = f"{server.base_url}/plainblog/2026/a-long-enough-post/"
    private = f"{server.base_url}/plainblog/private/hidden-post-slug/"
    foreign = "http://localhost/2026/hostile-entry-slug/"
    candidates = [Candidate(url=url, guid=url) for url in (own, private, foreign)]
    config = FetchConfig(min_delay_seconds=0.5, allow_private_hosts=["127.0.0.1"])
    async with Fetcher(conn, config) as fetcher:
        reasons = await discover.screen(fetcher, row, candidates)
    conn.close()
    assert reasons == {private: "robots"}


def test_failed_first_sync_keeps_no_validators_so_the_retry_refetches(
    env: dict[str, str], server: FixtureServer
) -> None:
    sitemap = "/feedblog/sitemap.xml"
    server.break_after(sitemap, 1)
    try:
        code, out = invoke(env, "poll", "--no-process", "--site", "feedblog", "--json")
    finally:
        server.heal(sitemap)
    assert code == 6, out
    with db(env) as conn:
        cached = conn.execute("SELECT url FROM http_cache").fetchall()
    assert cached == []
    server.reset_counts()
    report = by_slug(poll(env, "--site", "feedblog"))
    assert report["feedblog"]["first_sync"] is True
    assert ("GET", "/feedblog/feed.xml", 200) in server.requests
    assert stored_urls(env, "feedblog") == set(server.post_urls("feedblog"))
