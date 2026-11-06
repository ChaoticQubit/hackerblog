"""Shared value types for discovery (docs/architecture.md section 8.3)."""

from dataclasses import dataclass

URL_LENGTH_MAX = 2048
GUID_LENGTH_MAX = 2048
TITLE_LENGTH_MAX = 300
AUTHOR_LENGTH_MAX = 200


@dataclass(frozen=True)
class Candidate:
    """One post found by a feed, sitemap or link diff, before it is stored.

    url is absolute and canonical, source_url the absolute URL as the source gave it,
    guid the feed id or the canonical url, and published_at UTC ISO-8601 with a Z suffix.
    """

    url: str
    guid: str
    title: str | None = None
    author: str | None = None
    published_at: str | None = None
    source_url: str | None = None

    def __post_init__(self) -> None:
        assert self.url.startswith(("http://", "https://")), self.url
        assert self.guid, "guid must not be empty"
        assert len(self.url) <= URL_LENGTH_MAX
        assert len(self.guid) <= GUID_LENGTH_MAX
        assert self.title is None or len(self.title) <= TITLE_LENGTH_MAX
        assert self.author is None or len(self.author) <= AUTHOR_LENGTH_MAX
        assert self.source_url is None or len(self.source_url) <= URL_LENGTH_MAX
        assert self.published_at is None or self.published_at.endswith("Z")
        assert self.source_url is None or self.source_url.startswith(("http://", "https://"))
