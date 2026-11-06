import time
from pathlib import Path

import pytest

from hackerblog.core.discovery.feeds import (
    COMMON_FEED_PATHS,
    MAX_FEED_ENTRIES,
    feed_links,
    page_title,
    parse_feed,
)
from hackerblog.core.discovery.model import (
    AUTHOR_LENGTH_MAX,
    GUID_LENGTH_MAX,
    TITLE_LENGTH_MAX,
    URL_LENGTH_MAX,
    Candidate,
)

FIXTURES = Path(__file__).parent / "fixtures" / "discovery"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_rss2_entries_become_candidates() -> None:
    found = parse_feed(fixture("rss2.xml"), "https://blog.example.com/feed")
    assert found == [
        Candidate(
            url="https://blog.example.com/2024/05/breaking-xxe/",
            guid="tag:blog.example.com,2024:post-42",
            title="Breaking XXE in 2024",
            author="Ada Lovelace",
            published_at="2024-05-01T08:30:00Z",
            source_url="https://blog.example.com/2024/05/breaking-xxe/?utm_source=rss&utm_medium=feed",
        ),
        Candidate(
            url="https://blog.example.com/2024/04/relative-post/",
            guid="https://blog.example.com/2024/04/relative-post/",
            title="Relative link post",
            author=None,
            published_at="2024-04-15T08:00:00Z",
            source_url="https://blog.example.com/2024/04/relative-post/#comments",
        ),
    ]


def test_atom_entries_use_published_then_updated() -> None:
    found = parse_feed(fixture("atom.xml"), "https://atom.example.org/feed.atom")
    assert found is not None
    assert [c.url for c in found] == [
        "https://atom.example.org/posts/atom-entry-one",
        "https://atom.example.org/posts/atom-entry-two",
    ]
    assert found[0].guid == "urn:uuid:1225c695-cfb8-4ebb-aaaa-80da344efa6a"
    assert found[0].author == "Grace Hopper"
    assert found[0].published_at == "2024-06-02T12:00:00Z"
    assert found[1].published_at == "2024-05-31T01:15:00Z"


def test_json_feed_is_parsed() -> None:
    found = parse_feed(fixture("jsonfeed.json"), "https://json.example.net/feed.json")
    assert found == [
        Candidate(
            url="https://json.example.net/2024/07/first-json-post",
            guid="https://json.example.net/2024/07/first-json-post",
            title="First JSON post",
            author="Linus Example",
            published_at="2024-07-04T09:00:00Z",
            source_url="https://json.example.net/2024/07/first-json-post",
        )
    ]


def test_html_page_is_not_a_feed() -> None:
    assert parse_feed(fixture("home_with_feeds.html"), "https://blog.example.com/") is None
    assert parse_feed(b"", "https://blog.example.com/") is None
    assert parse_feed(b"<rss><channel></channel></rss>", "https://blog.example.com/") is None


def test_feed_entries_are_capped() -> None:
    items = "".join(
        f"<item><link>https://big.example.com/posts/entry-{i}</link></item>"
        for i in range(MAX_FEED_ENTRIES + 10)
    )
    body = f'<rss version="2.0"><channel>{items}</channel></rss>'.encode()
    found = parse_feed(body, "https://big.example.com/feed")
    assert found is not None
    assert len(found) == MAX_FEED_ENTRIES


def test_billion_laughs_feed_fails_closed_without_expanding() -> None:
    started = time.monotonic()
    found = parse_feed(fixture("billion_laughs_feed.xml"), "https://evil.example.com/feed")
    assert time.monotonic() - started < 5
    assert found is None


def test_external_entity_feed_fails_closed() -> None:
    found = parse_feed(fixture("xxe_feed.xml"), "https://evil.example.com/feed")
    assert found is None


def test_entity_text_inside_cdata_content_is_not_rejected() -> None:
    found = parse_feed(fixture("rss2.xml"), "https://blog.example.com/feed")
    assert found is not None
    assert len(found) == 2


def test_feed_links_in_document_order_absolute_and_deduped() -> None:
    links = feed_links(fixture("home_with_feeds.html"), "https://blog.example.com/blog/")
    assert links == [
        "https://blog.example.com/feed.xml",
        "https://blog.example.com/atom.xml",
        "https://blog.example.com/blog/feed.json",
    ]


def test_feed_links_empty_when_none() -> None:
    assert feed_links(b"<html><head></head><body>hi</body></html>", "https://a.example/") == []


def test_page_title_is_collapsed_and_bounded() -> None:
    assert page_title(fixture("home_with_feeds.html")) == "Example Security Blog & Notes"
    long_title = b"<title>" + b"x" * 200 + b"</title>"
    assert page_title(long_title) == "x" * 80
    assert page_title(b"<html><title>   </title></html>") is None
    assert page_title(b"<html><body>no title</body></html>") is None
    assert page_title("<title>Caf\xe9</title>".encode("cp1252")) == "Caf\xe9"


def test_common_feed_paths() -> None:
    assert COMMON_FEED_PATHS == ("/feed", "/rss.xml", "/atom.xml", "/index.xml", "/feed.xml")


def test_body_that_names_a_local_file_is_not_opened() -> None:
    path = bytes(FIXTURES / "rss2.xml")
    assert parse_feed(path, "https://blog.example.com/feed") is None


def test_deeply_nested_json_is_not_a_feed() -> None:
    body = (
        b'{"version": "https://jsonfeed.org/version/1.1", "items": '
        + b"[" * 200_000
        + b"]" * 200_000
        + b"}"
    )
    assert parse_feed(body, "https://a.example/") is None


@pytest.mark.parametrize(
    "name",
    [
        "billion_laughs_feed_utf16.xml",
        "billion_laughs_feed_utf16_nobom.xml",
        "billion_laughs_feed_comment.xml",
    ],
)
def test_entity_feeds_hidden_from_the_prolog_check_fail_closed(name: str) -> None:
    started = time.monotonic()
    found = parse_feed(fixture(name), "https://evil.example.com/feed")
    assert time.monotonic() - started < 5
    assert found is None


def test_any_doctype_in_an_xml_feed_fails_closed() -> None:
    body = (
        b'<?xml version="1.0"?><!DOCTYPE rss><rss version="2.0"><channel>'
        b"<item><link>https://a.example/2024/01/plain-post</link></item></channel></rss>"
    )
    assert parse_feed(body, "https://a.example/feed") is None


def _rss_item(link: str, guid: str = "", title: str = "", author: str = "") -> bytes:
    fields = f"<link>{link}</link>"
    fields += f"<guid>{guid}</guid>" if guid else ""
    fields += f"<title>{title}</title>" if title else ""
    fields += f"<author>{author}</author>" if author else ""
    return f'<rss version="2.0"><channel><item>{fields}</item></channel></rss>'.encode()


def test_oversized_urls_are_dropped() -> None:
    long_path = "/posts/" + "a" * URL_LENGTH_MAX
    assert parse_feed(_rss_item(f"https://a.example{long_path}"), "https://a.example/") is None
    tracked = "https://a.example/posts/ok?utm_source=" + "x" * URL_LENGTH_MAX
    assert parse_feed(_rss_item(tracked), "https://a.example/") is None


def test_oversized_title_and_author_are_truncated() -> None:
    body = _rss_item(
        "https://a.example/posts/long-fields",
        title="t" * (TITLE_LENGTH_MAX + 50),
        author="w" * (AUTHOR_LENGTH_MAX + 50),
    )
    found = parse_feed(body, "https://a.example/")
    assert found is not None
    assert found[0].title == "t" * TITLE_LENGTH_MAX
    assert found[0].author == "w" * AUTHOR_LENGTH_MAX


def test_oversized_guid_falls_back_to_the_canonical_url() -> None:
    body = _rss_item("https://a.example/posts/long-guid#x", guid="g" * (GUID_LENGTH_MAX + 1))
    found = parse_feed(body, "https://a.example/")
    assert found is not None
    assert found[0].guid == "https://a.example/posts/long-guid"


def test_source_url_keeps_the_link_as_published() -> None:
    found = parse_feed(fixture("rss2.xml"), "https://blog.example.com/feed")
    assert found is not None
    assert [c.source_url for c in found] == [
        "https://blog.example.com/2024/05/breaking-xxe/?utm_source=rss&utm_medium=feed",
        "https://blog.example.com/2024/04/relative-post/#comments",
    ]


def test_source_url_keeps_host_case_and_gets_a_lowercase_scheme() -> None:
    found = parse_feed(_rss_item("HTTPS://A.example/posts/shouty"), "https://a.example/")
    assert found is not None
    assert found[0].url == "https://a.example/posts/shouty"
    assert found[0].source_url == "https://A.example/posts/shouty"


def test_html_named_entities_in_a_feed_still_yield_posts() -> None:
    found = parse_feed(fixture("html_entities_feed.xml"), "https://blog.example/")
    assert found is not None
    assert [item.title for item in found] == [
        "Caf\u00e9 notes \u2014 part 1",
        "Unknown &bogus; entity stays text",
    ]


def test_bare_ampersands_in_a_feed_still_yield_posts() -> None:
    found = parse_feed(fixture("bare_ampersand_feed.xml"), "https://blog.example/")
    assert found is not None
    assert [item.title for item in found] == ["Cats & dogs & XML"]
    assert found[0].url == "https://blog.example/2026/cats-and-dogs/?a=1&b=2"
