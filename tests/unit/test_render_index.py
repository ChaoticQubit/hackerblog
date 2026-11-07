import re

import pytest
from tests.unit.test_render_lesson import parse

from hackerblog.core.render.index import IndexEntry, render_index


def entry(site: str, title: str, published: str, status: str = "unread") -> IndexEntry:
    folder = f"{site}/{published}-{title.lower().replace(' ', '-')}"
    return IndexEntry(
        site_slug=site,
        site_name=site.title(),
        title=title,
        published=published,
        lesson_href=f"{folder}/lesson.html",
        notes_href=f"{folder}/notes.md",
        reading_status=status,
    )


def test_index_groups_by_site_newest_first() -> None:
    html = render_index(
        [
            entry("alpha", "Old post", "2026-01-01"),
            entry("beta", "Beta post", "2026-05-01", "learned"),
            entry("alpha", "New post", "2026-03-01"),
        ]
    )
    assert html.index("Alpha") < html.index("Beta")
    assert html.index("New post") < html.index("Old post")
    assert 'href="alpha/2026-03-01-new-post/lesson.html"' in html
    assert 'href="alpha/2026-03-01-new-post/notes.md"' in html
    assert "learned" in html.lower()


def test_index_has_no_script_and_pins_style() -> None:
    html = render_index([entry("alpha", "A post", "2026-01-01")])
    doc = parse(html)
    assert not [t for t, _ in doc.tags if t == "script"]
    csp = next(a["content"] for t, a in doc.tags if t == "meta" and a.get("http-equiv"))
    assert csp is not None
    assert re.search(r"style-src 'sha256-[^']+'", csp)
    assert "script-src" not in csp


def test_empty_index_renders() -> None:
    assert "No lessons yet" in render_index([])


def test_index_entry_rejects_unsafe_href() -> None:
    with pytest.raises(AssertionError):
        IndexEntry("a", "A", "t", "2026-01-01", "javascript:alert(1)", "a/b/notes.md", "unread")
