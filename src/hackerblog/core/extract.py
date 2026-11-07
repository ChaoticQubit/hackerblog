"""Turn fetched post HTML into Markdown text plus metadata, offline, with trafilatura."""

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import trafilatura
from trafilatura.metadata import extract_metadata

MIN_WORDS = 150
CHARS_PER_TOKEN = 4
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
INJECTION_MARKERS = re.compile(
    r"ignore (all |any )?(the )?(previous|prior|above) (instructions|prompts?)"
    r"|disregard (all |the )?(previous|prior|above)"
    r"|system prompt"
    r"|you are now"
    r"|new instructions:"
    r"|developer mode",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Extracted:
    """Main text of one post as Markdown, its metadata, size, hash and reader flags."""

    title: str | None
    author: str | None
    published_at: str | None
    body_md: str
    word_count: int
    content_hash: str
    flags: tuple[str, ...]

    def __post_init__(self) -> None:
        assert self.word_count == len(self.body_md.split())
        assert len(self.content_hash) == 64


def content_hash(text: str) -> str:
    """Return the sha256 hex digest of text with every whitespace run collapsed to one space."""
    normalised = " ".join(text.split())
    digest = hashlib.sha256(normalised.encode("utf-8")).hexdigest()
    assert len(digest) == 64
    return digest


def injection_flags(text: str) -> tuple[str, ...]:
    """Return ("possible_injection",) when text contains a known prompt-injection marker.

    A hint shown to the reader, not a defence.
    """
    flagged = INJECTION_MARKERS.search(text) is not None
    return ("possible_injection",) if flagged else ()


def truncate(body_md: str, max_input_tokens: int) -> tuple[str, bool]:
    """Cut body_md to about max_input_tokens, at the last paragraph break before the limit.

    Returns the text and whether it was cut. Without a paragraph break it cuts at the limit.
    """
    assert max_input_tokens > 0
    limit = max_input_tokens * CHARS_PER_TOKEN
    if len(body_md) <= limit:
        return body_md, False
    boundary = body_md.rfind("\n\n", 0, limit + 1)
    cut = body_md[:boundary] if boundary > 0 else body_md[:limit]
    assert len(cut) <= limit
    return cut.rstrip(), True


def _iso_utc(raw: str | None) -> str | None:
    if not raw:
        return None
    for pattern in (DATE_FORMAT, "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw, pattern)
        except ValueError:
            continue
        aware = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        return aware.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def extract(html: bytes, url: str) -> Extracted:
    """Extract the main text of html as Markdown, plus title, author and published time.

    Works only on the given bytes and never fetches. Keeps headings, lists, tables and code
    blocks; drops navigation, footers and comments. published_at is UTC ISO-8601 or None.
    """
    assert url
    date_config = {"outputformat": DATE_FORMAT, "original_date": True, "extensive_search": False}
    tree = trafilatura.load_html(html)
    if tree is None:
        return Extracted(None, None, None, "", 0, content_hash(""), ())
    body = trafilatura.extract(
        tree,
        url=url,
        output_format="markdown",
        include_formatting=True,
        include_tables=True,
        include_links=False,
        include_images=False,
        include_comments=False,
        favor_precision=True,
        deduplicate=False,
        date_extraction_params=date_config,
    )
    body_md = (body or "").strip()
    meta = extract_metadata(tree, default_url=url, date_config=date_config)
    result = Extracted(
        title=(meta.title or None) if body_md else None,
        author=meta.author or None,
        published_at=_iso_utc(meta.date),
        body_md=body_md,
        word_count=len(body_md.split()),
        content_hash=content_hash(body_md),
        flags=injection_flags(body_md),
    )
    assert result.published_at is None or result.published_at.endswith("Z")
    return result
