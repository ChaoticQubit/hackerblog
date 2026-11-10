from pathlib import Path

import pytest

from hackerblog.core.discovery.linkdiff import extract_links, is_post_url
from hackerblog.core.discovery.model import URL_LENGTH_MAX

FIXTURES = Path(__file__).parent / "fixtures" / "discovery"


def test_extract_links_absolute_canonical_deduped_in_order() -> None:
    html = (FIXTURES / "home_with_feeds.html").read_bytes()
    assert extract_links(html, "https://blog.example.com/blog/") == [
        "https://blog.example.com/",
        "https://blog.example.com/about/",
        "https://blog.example.com/tag/xss/",
        "https://blog.example.com/2024/05/breaking-xxe/",
        "https://blog.example.com/blog/posts/a-long-enough-slug",
        "https://other.example.org/2024/01/elsewhere-post",
    ]


def test_extract_links_empty_page() -> None:
    assert extract_links(b"", "https://blog.example.com/") == []


ROOT = "https://blog.example.com/"
WRITING = "https://example.org/writing/"
HEURISTIC = [
    (ROOT, "https://blog.example.com/2024/05/breaking-xxe/", True),
    (ROOT, "https://blog.example.com/posts/short", True),
    (ROOT, "https://blog.example.com/a-long-single-slug", True),
    (ROOT, "https://BLOG.example.com/posts/short", True),
    (ROOT, "https://blog.example.com/", False),
    (ROOT, "https://blog.example.com", False),
    (ROOT, "https://blog.example.com/?page=2", False),
    (ROOT, "https://blog.example.com/short", False),
    (ROOT, "https://blog.example.com/tag/xss-and-friends", False),
    (ROOT, "https://blog.example.com/tags/xss", False),
    (ROOT, "https://blog.example.com/category/web/long-title-here", False),
    (ROOT, "https://blog.example.com/categories/web", False),
    (ROOT, "https://blog.example.com/author/ada-lovelace", False),
    (ROOT, "https://blog.example.com/page/2", False),
    (ROOT, "https://blog.example.com/feed/atom", False),
    (ROOT, "https://blog.example.com/search/everything", False),
    (ROOT, "https://blog.example.com/about/team", False),
    (ROOT, "https://blog.example.com/contact/form", False),
    (ROOT, "https://blog.example.com/images/diagram.PNG", False),
    (ROOT, "https://blog.example.com/files/paper.pdf", False),
    (ROOT, "https://blog.example.com/dl/source.tar.gz", False),
    (ROOT, "https://blog.example.com/dl/source.zip", False),
    (ROOT, "https://blog.example.com/static/site.css", False),
    (ROOT, "https://blog.example.com/static/app.js", False),
    (ROOT, "https://other.example.com/2024/05/breaking-xxe/", False),
    (ROOT, "https://sub.blog.example.com/2024/05/breaking-xxe/", False),
    (ROOT, "mailto:someone@example.com", False),
    (WRITING, "https://example.org/writing/my-first-post", True),
    (WRITING, "https://example.org/2023/01/moved-post", True),
    (WRITING, "https://example.org/products/widgets", False),
    (WRITING, "https://example.org/writing-tips-and-more", False),
    (WRITING, "https://example.org/writing/", False),
    (WRITING, "https://example.org/writing", False),
]


@pytest.mark.parametrize(("site", "url", "expected"), HEURISTIC)
def test_post_heuristic(site: str, url: str, expected: bool) -> None:
    assert is_post_url(url, site, None) is expected


PATTERN = r"^https://example\.org/writing/[0-9]{4}/[a-z0-9-]+/?$"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.org/writing/2024/hello-world/", True),
        ("https://example.org/writing/2024/hello-world", True),
        ("https://example.org/writing/drafts/hello-world", False),
        ("https://example.org/tag/2024/x", False),
        ("https://evil.example.net/?u=https://example.org/writing/2024/x", False),
    ],
)
def test_post_pattern(url: str, expected: bool) -> None:
    assert is_post_url(url, WRITING, PATTERN) is expected


def test_pattern_still_requires_same_host() -> None:
    assert not is_post_url("https://other.example.com/writing/x", WRITING, r"/writing/")


def test_extract_links_drops_links_over_the_url_cap() -> None:
    long_href = "/posts/" + "a" * URL_LENGTH_MAX
    html = f'<a href="{long_href}">x</a><a href="/posts/short">y</a>'.encode()
    assert extract_links(html, "https://a.example/") == ["https://a.example/posts/short"]


BEAR = "https://karpathy.bearblog.dev/blog/"
BEAR_ROOTS = ("/",)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://karpathy.bearblog.dev/verifiability/", True),
        ("https://karpathy.bearblog.dev/the-append-and-review-note/", True),
        ("https://karpathy.bearblog.dev/", False),
        ("https://karpathy.bearblog.dev/blog/", False),
        ("https://karpathy.bearblog.dev/blog/?q=pot-of-honey", False),
        ("https://karpathy.bearblog.dev/feed/", False),
        ("https://karpathy.bearblog.dev/feed/?type=rss", False),
        ("https://karpathy.bearblog.dev/tag/machine-learning/", False),
        ("https://karpathy.bearblog.dev/category/essays-and-notes/", False),
        ("https://karpathy.bearblog.dev/now/", False),
        ("https://bearblog.dev/a-long-enough-post-slug/", False),
    ],
)
def test_post_roots_widen_the_heuristic_but_exclude_non_post_pages(
    url: str, expected: bool
) -> None:
    assert is_post_url(url, BEAR, None, BEAR_ROOTS) is expected


def test_posts_outside_the_site_path_need_a_root() -> None:
    assert not is_post_url("https://karpathy.bearblog.dev/verifiability/", BEAR, None)


@pytest.mark.parametrize("url", ["https://example.org/writing/", "https://example.org/"])
def test_pattern_never_matches_the_site_page_or_home(url: str) -> None:
    assert not is_post_url(url, WRITING, r"example\.org/")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://shared.example/rootblog/", False),
        ("https://shared.example/rootblog/now/", False),
        ("https://shared.example/rootblog/tag/machine-learning/", False),
        ("https://shared.example/rootblog/a-short-one/", False),
        ("https://shared.example/rootblog/a-long-enough-slug/", True),
        ("https://shared.example/other/a-long-enough-slug/", False),
    ],
)
def test_depth_under_a_root_counts_from_the_root(url: str, expected: bool) -> None:
    site = "https://shared.example/rootblog/blog/"
    assert is_post_url(url, site, None, ("/rootblog/",)) is expected
