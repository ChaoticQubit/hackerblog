import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hackerblog.cli.main import app

from .server import FixtureServer, serve_blogs

runner = CliRunner()
FEED_PATH = "/agg/feed.xml"
FEED_SIZE = 30


@pytest.fixture(scope="module")
def server() -> Iterator[FixtureServer]:
    with serve_blogs() as running:
        yield running


def story(rank: int) -> str:
    return f"https://story-{rank}.example/2026/post-number-{rank}/"


def aggregator_feed(ranks: list[int]) -> str:
    """RSS in rank order whose pubDates grow with rank, so newest first is not rank order."""
    start = datetime(2026, 1, 1, tzinfo=UTC)
    items = "".join(
        f"<item><title>Story {rank}</title><link>{story(rank)}</link>"
        f'<guid isPermaLink="false">story-{rank}</guid>'
        f"<pubDate>{format_datetime(start + timedelta(hours=rank), usegmt=True)}</pubDate></item>\n"
        for rank in ranks
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel>\n'
        "<title>Aggregator</title><link>https://agg.example/</link>\n"
        f"<description>Links elsewhere</description>\n{items}</channel></rss>\n"
    )


@pytest.fixture
def env(tmp_path: Path, server: FixtureServer) -> dict[str, str]:
    values = {
        "HOME": str(tmp_path),
        "HACKERBLOG_HOME": str(tmp_path / "hb"),
        "HACKERBLOG_CONFIG": "",
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "NO_COLOR": "1",
    }
    result = runner.invoke(app, ["init", "--output-dir", str(tmp_path / "out")], env=values)
    assert result.exit_code == 0, result.output
    server.host(
        {FEED_PATH: (aggregator_feed(list(range(1, FEED_SIZE + 1))), "application/rss+xml")}
    )
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
            "[[site]]",
            f'url = "{server.base_url}/agg/"',
            'slug = "agg"',
            f'feed = "{server.base_url}{FEED_PATH}"',
            "aggregator = true",
            "limit = 10",
            "",
        ]
    )
    (tmp_path / "hb" / "sites.toml").write_text(config, encoding="utf-8")
    return values


def poll(env: dict[str, str]) -> Any:
    result = runner.invoke(app, ["poll", "--no-process", "--json"], env=env)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["data"]["sites"][0]


def posts_by_status(env: dict[str, str]) -> dict[tuple[str, str | None], set[str]]:
    with sqlite3.connect(Path(env["HACKERBLOG_HOME"]) / "hackerblog.db") as conn:
        rows = conn.execute("SELECT url, status, skip_reason FROM posts").fetchall()
    found: dict[tuple[str, str | None], set[str]] = {}
    for url, status, reason in rows:
        found.setdefault((status, reason), set()).add(url)
    return found


def test_aggregator_feed_with_limit_queues_the_top_ranks_and_skips_the_rest(
    env: dict[str, str], server: FixtureServer
) -> None:
    first = poll(env)
    assert first["error"] is None, first
    assert (first["first_sync"], first["queued"], first["skipped"]) == (True, 10, 20)
    stored = posts_by_status(env)
    assert stored == {
        ("queued", None): {story(rank) for rank in range(1, 11)},
        ("skipped", "over_limit"): {story(rank) for rank in range(11, 31)},
    }

    fresh = list(range(31, 36))
    server.host({FEED_PATH: (aggregator_feed([*fresh, *range(1, 26)]), "application/rss+xml")})
    second = poll(env)
    assert second["error"] is None, second
    assert (second["first_sync"], second["new"], second["queued"]) == (False, 5, 5)
    stored = posts_by_status(env)
    assert stored[("queued", None)] == {story(rank) for rank in [*range(1, 11), *fresh]}
    assert stored[("skipped", "over_limit")] == {story(rank) for rank in range(11, 31)}
