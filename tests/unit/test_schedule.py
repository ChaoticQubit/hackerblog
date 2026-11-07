from pathlib import Path

import pytest

from hackerblog.core.schedule import (
    jitter_minutes,
    poll_time,
    render_cron,
    render_launchd,
    render_systemd,
    site_cron,
)


@pytest.mark.parametrize(
    ("digest", "expected"),
    [("08:00", (7, 30)), ("00:10", (23, 40)), ("23:45", (23, 15)), ("00:30", (0, 0))],
)
def test_poll_time_is_thirty_minutes_before_digest(digest: str, expected: tuple[int, int]) -> None:
    assert poll_time(digest) == expected


def test_jitter_is_stable_and_bounded() -> None:
    values = {jitter_minutes(f"site-{n}") for n in range(200)}
    assert jitter_minutes("portswigger-net-research") == jitter_minutes("portswigger-net-research")
    assert values <= set(range(21))
    assert len(values) > 10


def test_site_cron_for_each_schedule_kind() -> None:
    jitter = jitter_minutes("s")
    hour, minute = divmod(7 * 60 + 30 + jitter, 60)
    assert site_cron("daily", "s", "08:00") == f"{minute} {hour % 24} * * *"
    assert site_cron("weekly", "s", "08:00") == f"{minute} {hour % 24} * * 1"
    assert site_cron("hourly", "s", "08:00") == f"{jitter} * * * *"
    assert site_cron("15 6 * * 1-5", "s", "08:00") == "15 6 * * 1-5"


def test_daily_jitter_wraps_past_midnight() -> None:
    slug = next(f"s{n}" for n in range(1000) if jitter_minutes(f"s{n}") >= 15)
    minute = (23 * 60 + 50 + jitter_minutes(slug)) % (24 * 60)
    assert site_cron("daily", slug, "00:20") == f"{minute % 60} {minute // 60} * * *"


def test_launchd_plist_escapes_paths() -> None:
    plist = render_launchd(Path("/Users/a&b/bin/hackerblog"), Path("/Users/a&b/logs"), 7, 5)
    assert "<string>/Users/a&amp;b/bin/hackerblog</string>" in plist
    assert "<key>Hour</key><integer>7</integer>" in plist
    assert "<key>Minute</key><integer>5</integer>" in plist
    assert "<string>poll</string>" in plist
    assert "&b/" not in plist


def test_cron_line_quotes_paths() -> None:
    line = render_cron(Path("/opt/my apps/hackerblog"), Path("/var/log/hb.log"), 7, 30)
    assert line == "30 7 * * * '/opt/my apps/hackerblog' poll >> /var/log/hb.log 2>&1"


def test_systemd_units() -> None:
    service, timer = render_systemd(Path('/opt/my "x"/hackerblog'), 7, 30)
    assert "Type=oneshot" in service
    assert 'ExecStart="/opt/my \\"x\\"/hackerblog" poll' in service
    assert "OnCalendar=*-*-* 07:30:00" in timer
    assert "Persistent=true" in timer
