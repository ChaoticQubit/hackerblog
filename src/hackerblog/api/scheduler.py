"""APScheduler 3.11 jobs built from sites.toml, each calling the injected poll entry point."""

import functools
import importlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from hackerblog.core.config import Config
from hackerblog.core.discovery import service as discovery
from hackerblog.core.paths import Paths
from hackerblog.core.schedule import site_cron

PollFn = Callable[[Sequence[str]], Awaitable[object]]
MISFIRE_GRACE_S = 6 * 3600
WEEKDAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
logger = logging.getLogger("hackerblog.scheduler")
_asyncio: Any = importlib.import_module("apscheduler.schedulers.asyncio")
_cron: Any = importlib.import_module("apscheduler.triggers.cron")


def weekday_field(field: str) -> str:
    """Translate a standard cron day-of-week field (0 or 7 = Sunday) to APScheduler names.

    APScheduler 3 numbers weekdays from Monday, so numbers are expanded and named.
    """
    if field == "*":
        return field
    days: set[int] = set()
    for part in field.split(","):
        span, _, step_text = part.partition("/")
        step = int(step_text) if step_text else 1
        if span == "*":
            first, last = 0, 6
        else:
            first_text, _, last_text = span.partition("-")
            first, last = int(first_text), int(last_text or first_text)
        assert 0 <= first <= last <= 7 and step > 0, field
        days.update(day % 7 for day in range(first, last + 1, step))
    named = ",".join(WEEKDAYS[day] for day in sorted(days))
    assert named
    return named


def cron_trigger(expression: str, timezone: str | None) -> Any:
    """Build a CronTrigger from a validated 5-field expression in standard cron numbering.

    Unlike cron, APScheduler requires both day fields when both are restricted.
    """
    minute, hour, day, month, weekday = expression.split()
    options: dict[str, Any] = {"timezone": timezone} if timezone else {}
    return _cron.CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=weekday_field(weekday),
        **options,
    )


def discovery_poll(paths: Paths) -> PollFn:
    """Return the poll entry point scheduled jobs call: discovery only, recorded as scheduler.

    Posts are queued, not processed, the same as `hackerblog poll --no-process`.
    """
    return functools.partial(discovery.poll, paths, trigger="scheduler")


async def run_poll(poll: PollFn, slug: str) -> None:
    """Run one scheduled poll for slug; failures are logged so the scheduler keeps going."""
    try:
        await poll([slug])
    except Exception:
        logger.exception("poll.failed", extra={"site": slug})


def build_scheduler(config: Config, poll: PollFn) -> Any:
    """Return an unstarted AsyncIOScheduler with one coalescing job per enabled site."""
    timezone = config.notify.timezone
    scheduler: Any = (
        _asyncio.AsyncIOScheduler(timezone=timezone) if timezone else _asyncio.AsyncIOScheduler()
    )
    for site in config.sites:
        assert site.slug is not None
        enabled = config.defaults.enabled if site.enabled is None else site.enabled
        if not enabled:
            continue
        schedule = site.schedule or config.defaults.schedule
        expression = site_cron(schedule, site.slug, config.notify.digest_time)
        scheduler.add_job(
            run_poll,
            cron_trigger(expression, timezone),
            args=[poll, site.slug],
            id=f"poll:{site.slug}",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=MISFIRE_GRACE_S,
        )
    return scheduler
