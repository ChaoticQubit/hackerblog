import asyncio
from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from hackerblog.api.scheduler import build_scheduler, cron_trigger, run_poll, weekday_field
from hackerblog.core.config import Config

BERLIN = ZoneInfo("Europe/Berlin")


def config(**site_overrides: object) -> Config:
    return Config.model_validate(
        {
            "version": 1,
            "notify": {"timezone": "Europe/Berlin", "digest_time": "08:00"},
            "site": [
                {"url": "https://a.example/blog", **site_overrides},
                {"url": "https://b.example/", "enabled": False},
                {"url": "https://c.example/", "schedule": "hourly"},
            ],
        }
    )


async def noop(slugs: Sequence[str]) -> None:
    assert slugs


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("*", "*"),
        ("1", "mon"),
        ("0", "sun"),
        ("7", "sun"),
        ("1-5", "mon,tue,wed,thu,fri"),
        ("0-6/2", "sun,tue,thu,sat"),
        ("5-7", "sun,fri,sat"),
        ("*/3", "sun,wed,sat"),
    ],
)
def test_weekday_field_translates_cron_numbering(field: str, expected: str) -> None:
    assert weekday_field(field) == expected


def test_weekly_fires_on_monday() -> None:
    trigger = cron_trigger("0 7 * * 1", "Europe/Berlin")
    start = datetime(2026, 11, 6, 12, 0, tzinfo=BERLIN)
    fire = trigger.get_next_fire_time(None, start)
    assert fire.weekday() == 0
    assert (fire.hour, fire.minute) == (7, 0)


def test_monthly_job_fires_on_the_first_before_digest() -> None:
    scheduler = build_scheduler(config(schedule="monthly"), noop)
    trigger = scheduler.get_job("poll:a-example-blog").trigger
    fire = trigger.get_next_fire_time(None, datetime(2026, 11, 6, 12, 0, tzinfo=BERLIN))
    assert (fire.year, fire.month, fire.day, fire.hour) == (2026, 12, 1, 7)
    assert 30 <= fire.minute <= 50


def test_one_job_per_enabled_site() -> None:
    scheduler = build_scheduler(config(), noop)
    ids = sorted(job.id for job in scheduler.get_jobs())
    assert ids == ["poll:a-example-blog", "poll:c-example"]
    job = scheduler.get_job("poll:a-example-blog")
    assert job.coalesce is True
    assert job.max_instances == 1
    assert job.misfire_grace_time == 6 * 3600
    assert job.args[1] == "a-example-blog"


def test_daily_job_runs_before_digest_in_site_timezone() -> None:
    scheduler = build_scheduler(config(schedule="daily"), noop)
    trigger = scheduler.get_job("poll:a-example-blog").trigger
    fire = trigger.get_next_fire_time(None, datetime(2026, 11, 6, 0, 0, tzinfo=BERLIN))
    assert fire.hour == 7
    assert 30 <= fire.minute <= 50


def test_run_poll_calls_poll_with_slug_and_survives_errors() -> None:
    calls: list[list[str]] = []

    async def record(slugs: Sequence[str]) -> None:
        calls.append(list(slugs))
        raise RuntimeError("site down")

    asyncio.run(run_poll(record, "a-example-blog"))
    assert calls == [["a-example-blog"]]
