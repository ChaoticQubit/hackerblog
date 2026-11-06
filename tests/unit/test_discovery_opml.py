from pathlib import Path

import pytest

from hackerblog.core.discovery.opml import OpmlEntry, parse_opml

FIXTURES = Path(__file__).parent / "fixtures" / "discovery"


def test_reader_export_with_nested_folders() -> None:
    entries, invalid = parse_opml((FIXTURES / "feedly.opml").read_bytes())
    assert entries == [
        OpmlEntry(
            url="https://portswigger.net/research",
            feed="https://portswigger.net/research/rss",
            title="PortSwigger Research",
        ),
        OpmlEntry(
            url="https://googleprojectzero.blogspot.com/feeds/posts/default",
            feed="https://googleprojectzero.blogspot.com/feeds/posts/default",
            title="Project Zero",
        ),
        OpmlEntry(
            url="https://blog.cloudflare.com/",
            feed="https://blog.cloudflare.com/rss/",
            title="The Cloudflare Blog",
        ),
        OpmlEntry(
            url="https://y.example.com/feed",
            feed="https://y.example.com/feed",
            title="Bad html, good feed",
        ),
    ]
    assert len(invalid) == 1
    assert "Gopher thing" in invalid[0]


def test_lowercase_attribute_names_are_accepted() -> None:
    body = b'<opml><body><outline text="T" xmlurl="https://a.example.com/rss"/></body></opml>'
    entries, invalid = parse_opml(body)
    assert entries == [OpmlEntry("https://a.example.com/rss", "https://a.example.com/rss", "T")]
    assert invalid == []


@pytest.mark.parametrize("body", [b"", b"<rss/>", b"<opml><body>"])
def test_not_opml_raises_value_error(body: bytes) -> None:
    with pytest.raises(ValueError, match=r"."):
        parse_opml(body)


def test_external_entity_opml_fails_closed() -> None:
    with pytest.raises(ValueError, match="DTD"):
        parse_opml((FIXTURES / "xxe.opml").read_bytes())
