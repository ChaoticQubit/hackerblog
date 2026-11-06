import time
from pathlib import Path

import pytest

from hackerblog.core.discovery.sitemaps import MAX_SITEMAP_ENTRIES, Sitemap, parse_sitemap

FIXTURES = Path(__file__).parent / "fixtures" / "discovery"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_urlset_pages_with_lastmod_in_utc() -> None:
    assert parse_sitemap(fixture("sitemap.xml")) == Sitemap(
        pages=[
            ("https://blog.example.com/2024/05/breaking-xxe/", "2024-05-01T08:30:00Z"),
            ("https://blog.example.com/2023/11/old-post", "2023-11-20T00:00:00Z"),
            ("https://blog.example.com/about/", None),
            ("https://blog.example.com/bad-date-post", None),
        ],
        children=[],
    )


def test_sitemap_index_lists_children() -> None:
    assert parse_sitemap(fixture("sitemap_index.xml")) == Sitemap(
        pages=[],
        children=[
            "https://blog.example.com/post-sitemap.xml",
            "https://blog.example.com/page-sitemap.xml",
        ],
    )


def test_sitemap_entries_are_capped() -> None:
    urls = "".join(
        f"<url><loc>https://a.example.com/p{i}</loc></url>" for i in range(MAX_SITEMAP_ENTRIES + 5)
    )
    body = f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    assert len(parse_sitemap(body.encode()).pages) == MAX_SITEMAP_ENTRIES


@pytest.mark.parametrize(
    "body",
    [b"", b"not xml", b"<html><body>hi</body></html>", b"<urlset><url><loc>x</url>"],
)
def test_non_sitemaps_raise_value_error(body: bytes) -> None:
    with pytest.raises(ValueError, match=r"."):
        parse_sitemap(body)


def test_billion_laughs_sitemap_fails_closed() -> None:
    started = time.monotonic()
    with pytest.raises(ValueError, match="DTD"):
        parse_sitemap(fixture("billion_laughs_sitemap.xml"))
    assert time.monotonic() - started < 5


def test_external_entity_sitemap_fails_closed() -> None:
    with pytest.raises(ValueError, match="DTD"):
        parse_sitemap(fixture("xxe_sitemap.xml"))


def test_doctype_hidden_behind_a_prolog_comment_is_refused() -> None:
    body = (
        b'<?xml version="1.0"?>\n<!-- <a> -->\n<!DOCTYPE urlset [<!ENTITY x "y">]>\n'
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>&x;</loc></url>'
        b"</urlset>"
    )
    with pytest.raises(ValueError, match="DTD"):
        parse_sitemap(body)


def test_lastmod_that_overflows_in_utc_is_dropped() -> None:
    body = (
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url>'
        b"<loc>https://a.example/post/</loc><lastmod>0001-01-01T00:00:00+14:00</lastmod>"
        b"</url></urlset>"
    )
    assert parse_sitemap(body).pages == [("https://a.example/post/", None)]
