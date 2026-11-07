"""Render index.html at the output root: every ready post grouped by site, newest first."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import groupby

from hackerblog.core.render.lesson import LESSON_CSS
from hackerblog.core.render.sanitize import ENV, csp_hash

INDEX_CSP = (
    f"default-src 'none'; style-src {csp_hash(LESSON_CSS)}; img-src data:; font-src data:; "
    "connect-src 'none'; form-action 'none'; base-uri 'none'"
)
POST_HREF = re.compile(
    r"^[a-z0-9][a-z0-9-]*/\d{4}-\d{2}-\d{2}-[a-z0-9-]+/(lesson\.html|notes\.md)$"
)
READING = {"unread": "Unread", "read": "Read", "learned": "Learned", "archived": "Archived"}


@dataclass(frozen=True)
class IndexEntry:
    """One ready post in the library index; hrefs are relative to the output root."""

    site_slug: str
    site_name: str
    title: str
    published: str
    lesson_href: str
    notes_href: str
    reading_status: str

    def __post_init__(self) -> None:
        assert POST_HREF.fullmatch(self.lesson_href), f"bad href {self.lesson_href!r}"
        assert POST_HREF.fullmatch(self.notes_href), f"bad href {self.notes_href!r}"
        assert self.reading_status in READING


def render_index(entries: Sequence[IndexEntry]) -> str:
    """Render the script-free, CSP-pinned library index.html."""
    by_site = sorted(entries, key=lambda e: (e.site_name.lower(), e.site_slug))
    groups = [
        (site, sorted(posts, key=lambda e: (e.published, e.title), reverse=True))
        for site, posts in groupby(by_site, key=lambda e: e.site_slug)
    ]
    page = ENV.get_template("index.html.j2").render(
        csp=INDEX_CSP,
        css=LESSON_CSS,
        groups=groups,
        total=len(entries),
        reading=READING,
    )
    assert "<script" not in page
    assert sum(len(posts) for _, posts in groups) == len(entries)
    return page
