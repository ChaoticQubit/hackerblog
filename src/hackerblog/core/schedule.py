"""Poll times per site and the cron, launchd and systemd files `schedule show` prints."""

import hashlib
import shlex
from pathlib import Path
from xml.sax.saxutils import escape

POLL_LEAD_MINUTES = 30
JITTER_MAX_MINUTES = 20
DAY_MINUTES = 24 * 60
LAUNCHD_LABEL = "dev.hackerblog.poll"


def poll_time(digest_time: str) -> tuple[int, int]:
    """Return (hour, minute) 30 minutes before digest_time "HH:MM", wrapping at midnight."""
    hours, minutes = (int(part) for part in digest_time.split(":"))
    assert 0 <= hours < 24 and 0 <= minutes < 60
    total = (hours * 60 + minutes - POLL_LEAD_MINUTES) % DAY_MINUTES
    return divmod(total, 60)


def jitter_minutes(slug: str) -> int:
    """Return a stable 0-20 minute offset derived from slug, spreading sites apart."""
    assert slug
    value = int.from_bytes(hashlib.sha256(slug.encode()).digest()[:4], "big")
    return value % (JITTER_MAX_MINUTES + 1)


def site_cron(schedule: str, slug: str, digest_time: str) -> str:
    """Return the 5-field crontab expression for one site's schedule.

    hourly runs at the slug's jitter minute; daily, weekly (Mondays) and monthly (the 1st)
    run at the poll time plus jitter; any other value is already a validated cron expression.
    """
    jitter = jitter_minutes(slug)
    if schedule == "hourly":
        return f"{jitter} * * * *"
    hour, minute = poll_time(digest_time)
    total = (hour * 60 + minute + jitter) % DAY_MINUTES
    at = f"{total % 60} {total // 60}"
    if schedule == "daily":
        return f"{at} * * *"
    if schedule == "weekly":
        return f"{at} * * 1"
    if schedule == "monthly":
        return f"{at} 1 * *"
    assert len(schedule.split()) == 5, schedule
    return schedule


def render_launchd(executable: Path, logs: Path, hour: int, minute: int) -> str:
    """Return a LaunchAgent plist that runs `hackerblog poll` daily at hour:minute."""
    assert 0 <= hour < 24 and 0 <= minute < 60
    exe = escape(str(executable))
    out = escape(str(logs / "poll.out.log"))
    err = escape(str(logs / "poll.err.log"))
    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
            '<plist version="1.0">',
            "<dict>",
            f"  <key>Label</key><string>{LAUNCHD_LABEL}</string>",
            "  <key>ProgramArguments</key>",
            "  <array>",
            f"    <string>{exe}</string>",
            "    <string>poll</string>",
            "  </array>",
            "  <key>StartCalendarInterval</key>",
            f"  <dict><key>Hour</key><integer>{hour}</integer>"
            f"<key>Minute</key><integer>{minute}</integer></dict>",
            f"  <key>StandardOutPath</key><string>{out}</string>",
            f"  <key>StandardErrorPath</key><string>{err}</string>",
            "</dict>",
            "</plist>",
            "",
        ]
    )


def render_cron(executable: Path, log: Path, hour: int, minute: int) -> str:
    """Return one crontab line that runs `hackerblog poll` daily, appending to log."""
    assert 0 <= hour < 24 and 0 <= minute < 60
    return (
        f"{minute} {hour} * * * {shlex.quote(str(executable))} poll >> {shlex.quote(str(log))} 2>&1"
    )


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_systemd(executable: Path, hour: int, minute: int) -> tuple[str, str]:
    """Return (hackerblog-poll.service, hackerblog-poll.timer) for a daily oneshot poll."""
    assert 0 <= hour < 24 and 0 <= minute < 60
    service = "\n".join(
        [
            "[Unit]",
            "Description=HackerBlog poll",
            "",
            "[Service]",
            "Type=oneshot",
            f"ExecStart={_systemd_quote(str(executable))} poll",
            "",
        ]
    )
    timer = "\n".join(
        [
            "[Unit]",
            "Description=Run HackerBlog poll daily",
            "",
            "[Timer]",
            f"OnCalendar=*-*-* {hour:02d}:{minute:02d}:00",
            "Persistent=true",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
    )
    return service, timer
