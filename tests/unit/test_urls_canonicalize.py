import pytest

from hackerblog.core.net.urls import canonicalize

CASES = [
    ("HTTPS://Blog.Example.COM/Post", "https://blog.example.com/Post"),
    ("http://example.com:80/a", "http://example.com/a"),
    ("https://example.com:443/a", "https://example.com/a"),
    ("https://example.com:8443/a", "https://example.com:8443/a"),
    ("http://example.com:443/a", "http://example.com:443/a"),
    ("https://example.com/a#section-2", "https://example.com/a"),
    ("https://example.com/a?utm_source=x&utm_medium=y", "https://example.com/a"),
    ("https://example.com/a?UTM_Campaign=x&id=3", "https://example.com/a?id=3"),
    ("https://example.com/a?fbclid=1&gclid=2&ref=hn&b=2", "https://example.com/a?b=2"),
    ("https://example.com/a?referrer=x", "https://example.com/a?referrer=x"),
    ("https://example.com/a?z=1&a=2&m=3", "https://example.com/a?a=2&m=3&z=1"),
    ("https://example.com/a?b=2&a=1&b=1", "https://example.com/a?a=1&b=2&b=1"),
    ("https://example.com/a?q=hello%20world", "https://example.com/a?q=hello%20world"),
    ("https://example.com//a///b//", "https://example.com/a/b/"),
    ("https://example.com/a/", "https://example.com/a/"),
    ("https://example.com/a", "https://example.com/a"),
    ("https://example.com", "https://example.com/"),
    ("https://example.com?", "https://example.com/"),
    ("https://[2001:DB8::1]:443/x", "https://[2001:db8::1]/x"),
    ("https://example.com/a?", "https://example.com/a"),
]


@pytest.mark.parametrize(("raw", "expected"), CASES)
def test_canonicalize_table(raw: str, expected: str) -> None:
    assert canonicalize(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), CASES)
def test_canonicalize_is_idempotent(raw: str, expected: str) -> None:
    assert canonicalize(expected) == expected
    assert canonicalize(canonicalize(raw)) == canonicalize(raw)


@pytest.mark.parametrize(
    "raw", ["ftp://example.com/a", "mailto:a@example.com", "https:///no-host", "http://[::1"]
)
def test_canonicalize_rejects_non_http_urls(raw: str) -> None:
    with pytest.raises(ValueError, match=r"."):
        canonicalize(raw)
