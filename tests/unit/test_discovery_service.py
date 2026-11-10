import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hackerblog.core.config import Config
from hackerblog.core.discovery.service import select_sites, toml_block
from hackerblog.core.store import sites
from hackerblog.core.store.migrate import connect, migrate


def test_toml_block_round_trips_hostile_titles() -> None:
    name = 'Emoji 🚀 "quoted" \\ back\nline\ttab\x7fdel\x00nul \u2028sep'
    text = toml_block({"url": "https://a.example/", "name": name, "tags": ["x", "y"], "n": 0})
    parsed = tomllib.loads(text)
    assert parsed["site"] == [
        {"url": "https://a.example/", "name": name, "tags": ["x", "y"], "n": 0}
    ]


def test_toml_block_skips_unset_values() -> None:
    text = toml_block({"url": "https://a.example/", "name": None, "tags": []})
    assert text == '[[site]]\nurl = "https://a.example/"\n'


def test_external_poll_skips_weekly_and_monthly_sites_polled_recently(tmp_path: Path) -> None:
    config = Config.model_validate(
        {
            "version": 1,
            "site": [
                {"url": "https://d.example/", "schedule": "daily"},
                {"url": "https://w.example/", "schedule": "weekly"},
                {"url": "https://m.example/", "schedule": "monthly"},
            ],
        }
    )
    conn = connect(tmp_path / "hb.db")
    migrate(conn)
    sites.sync_sites(conn, config.sites, sites.utc_now())
    ten_days_ago = (datetime.now(UTC) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for row in sites.list_sites(conn, include_all=False):
        sites.record_poll(conn, row.id, ten_days_ago, None)
    due = [row.slug for row, _ in select_sites(conn, config, ())]
    assert due == ["d-example", "w-example"]
