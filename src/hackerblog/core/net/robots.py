"""robots.txt verdicts parsed with protego and cached per origin in robots_cache."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cached_property
from typing import Literal
from urllib.parse import urlsplit

from protego import Protego

Verdict = Literal["parsed", "allow_all", "disallow_all"]
ROBOTS_TTL = timedelta(hours=24)
SITEMAPS_MAX = 50


@dataclass(frozen=True)
class RobotsPolicy:
    """What robots.txt says for one origin: parsed rules, allow all or disallow all."""

    verdict: Verdict
    body: str | None

    def __post_init__(self) -> None:
        assert self.verdict in {"parsed", "allow_all", "disallow_all"}
        assert (self.body is not None) == (self.verdict == "parsed")

    @cached_property
    def _rules(self) -> Protego | None:
        return None if self.body is None else Protego.parse(self.body)

    def allows(self, url: str, user_agent: str) -> bool:
        """Return whether user_agent may fetch url under this policy."""
        assert user_agent
        if self._rules is None:
            return self.verdict == "allow_all"
        allowed = self._rules.can_fetch(url, user_agent)
        assert isinstance(allowed, bool)
        return allowed

    def delay_s(self, user_agent: str) -> float:
        """Return the larger of Crawl-delay and Request-rate's gap for user_agent, or 0."""
        assert user_agent
        rules = self._rules
        if rules is None:
            return 0.0
        delay_s = float(rules.crawl_delay(user_agent) or 0.0)
        rate = rules.request_rate(user_agent)
        if rate is not None and rate.requests > 0:
            delay_s = max(delay_s, rate.seconds / rate.requests)
        assert delay_s >= 0
        return delay_s

    def sitemaps(self) -> list[str]:
        """Return the Sitemap URLs listed, at most SITEMAPS_MAX."""
        if self._rules is None:
            return []
        found = [str(url) for url in self._rules.sitemaps][:SITEMAPS_MAX]
        assert len(found) <= SITEMAPS_MAX
        return found


def origin_of(url: str) -> str:
    """Return scheme://host[:port] of url in lowercase, the key for robots and politeness."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    assert parts.scheme in {"http", "https"}
    assert host
    if ":" in host:
        host = f"[{host}]"
    return (
        f"{parts.scheme}://{host}"
        if parts.port is None
        else f"{parts.scheme}://{host}:{parts.port}"
    )


def load_policy(conn: sqlite3.Connection, origin: str, now: datetime) -> RobotsPolicy | None:
    """Return the cached policy for origin if it was fetched less than 24 hours before now."""
    assert now.tzinfo is not None
    row = conn.execute(
        "SELECT body, policy, fetched_at FROM robots_cache WHERE origin = ?", (origin,)
    ).fetchone()
    if row is None:
        return None
    body, verdict, fetched_at = row
    if now - datetime.fromisoformat(fetched_at) >= ROBOTS_TTL:
        return None
    assert verdict in {"parsed", "allow_all"}
    return RobotsPolicy(verdict, body)


def store_policy(
    conn: sqlite3.Connection, origin: str, policy: RobotsPolicy, now: datetime
) -> None:
    """Cache policy for origin as of now. disallow_all only lasts one poll, so never stored."""
    assert policy.verdict != "disallow_all"
    assert now.tzinfo is not None
    conn.execute(
        "INSERT INTO robots_cache (origin, body, policy, fetched_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (origin) DO UPDATE SET body = excluded.body, policy = excluded.policy, "
        "fetched_at = excluded.fetched_at",
        (origin, policy.body, policy.verdict, now.strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
