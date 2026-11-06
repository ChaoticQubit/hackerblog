"""Feed parsing and feed discovery in HTML pages (sections 8.2 and 8.3), no network."""

import html.entities
import importlib
import io
import json
import re
import time
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any, cast
from urllib.parse import urljoin

import feedparser

from hackerblog.core.discovery.model import (
    AUTHOR_LENGTH_MAX,
    GUID_LENGTH_MAX,
    TITLE_LENGTH_MAX,
    URL_LENGTH_MAX,
    Candidate,
)
from hackerblog.core.net.urls import canonicalize

COMMON_FEED_PATHS = ("/feed", "/rss.xml", "/atom.xml", "/index.xml", "/feed.xml")
FEED_TYPES = frozenset({"application/rss+xml", "application/atom+xml", "application/feed+json"})
MAX_FEED_ENTRIES = 5000
MAX_FEED_LINKS = 100
PAGE_TITLE_LENGTH_MAX = 80
ROOT_ELEMENT = re.compile(rb"<[A-Za-z_]")
WHITESPACE = re.compile(r"\s+")
AMPERSAND = re.compile(rb"&(#[0-9]{1,7};|#x[0-9A-Fa-f]{1,6};|[A-Za-z][A-Za-z0-9]{0,31};)?")
XML_ENTITY_NAMES = frozenset({b"amp;", b"lt;", b"gt;", b"quot;", b"apos;"})
etree: Any = importlib.import_module("lxml.etree")


def decode_html(body: bytes) -> str:
    """Decode an HTML or XML body as UTF-8, falling back to cp1252 for legacy pages."""
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("cp1252", errors="replace")


def xml_prolog(body: bytes) -> bytes:
    """Return the bytes before the first element start tag, where a DTD would have to be."""
    match = ROOT_ELEMENT.search(body)
    prolog = body[: match.start()] if match else body
    assert len(prolog) <= len(body)
    return prolog


def _xml_reference(match: re.Match[bytes]) -> bytes:
    reference = match.group(1)
    if reference is None:
        return b"&amp;"
    if reference.startswith(b"#") or reference in XML_ENTITY_NAMES:
        return match.group(0)
    text = html.entities.html5.get(reference.decode("ascii"))
    if text is None:
        return b"&amp;" + reference
    numeric = b"".join(b"&#%d;" % ord(char) for char in text)
    assert numeric.startswith(b"&#")
    return numeric


def xml_escapes(body: bytes) -> bytes:
    """Rewrite HTML named references as numeric ones and escape bare ampersands.

    Real feeds often carry &nbsp; or a lone &, which strict XML refuses. Only
    ASCII-compatible encodings match; other bodies come back unchanged.
    """
    escaped = AMPERSAND.sub(_xml_reference, body)
    assert len(escaped) >= len(body)
    return escaped


def parse_xml(body: bytes) -> Any:
    """Parse untrusted XML bytes and return the root element.

    Entities and network access are off and huge trees are refused. Any DTD fails closed,
    so billion-laughs and external-entity documents raise ValueError, as does bad XML. A
    body that fails only on HTML entities or bare ampersands is retried after xml_escapes.
    """
    if b"<!DOCTYPE" in xml_prolog(body).upper():
        raise ValueError("XML with a DTD is refused")
    parser: Any = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        huge_tree=False,
        load_dtd=False,
        dtd_validation=False,
        recover=False,
    )
    try:
        root: Any = etree.fromstring(body, parser=parser)
    except etree.XMLSyntaxError as error:
        escaped = xml_escapes(body)
        if escaped == body:
            raise ValueError(f"XML does not parse: {error}") from error
        try:
            root = etree.fromstring(escaped, parser=parser)
        except etree.XMLSyntaxError as retry_error:
            raise ValueError(f"XML does not parse: {retry_error}") from retry_error
    assert root is not None
    tree: Any = root.getroottree()
    doctype: object = tree.docinfo.doctype
    if doctype:
        raise ValueError("XML with a DTD is refused")
    return root


def utc_iso(text: str | None) -> str | None:
    """Convert an ISO-8601 or W3C date-time to UTC "YYYY-MM-DDTHH:MM:SSZ", or None.

    A date without a time means midnight UTC, and a time without an offset is taken as UTC.
    """
    if text is None:
        return None
    try:
        moment = datetime.fromisoformat(text.strip())
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        converted = moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OverflowError):
        return None
    assert len(converted) == 20, converted
    return converted


def _struct_iso(parsed: object) -> str | None:
    if not isinstance(parsed, time.struct_time):
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", parsed)


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return WHITESPACE.sub(" ", value).strip() or None


def _bounded(value: object, limit: int) -> str | None:
    text = _text(value)
    if text is None:
        return None
    bounded = text[:limit].rstrip()
    assert 0 < len(bounded) <= limit
    return bounded


def _candidate(entry: Any, base_url: str) -> Candidate | None:
    link = _text(entry.get("link"))
    if link is None:
        return None
    source_url = urljoin(base_url, link)
    try:
        url = canonicalize(source_url)
    except ValueError:
        return None
    if len(source_url) > URL_LENGTH_MAX or len(url) > URL_LENGTH_MAX:
        return None
    assert source_url.startswith(("http://", "https://")), source_url
    guid = _text(entry.get("id"))
    if guid is None or len(guid) > GUID_LENGTH_MAX:
        guid = url
    published = entry.get("published_parsed") or entry.get("updated_parsed")
    published_at = entry.get("published_iso") or _struct_iso(published)
    return Candidate(
        url=url,
        guid=guid,
        title=_bounded(entry.get("title"), TITLE_LENGTH_MAX),
        author=_bounded(entry.get("author"), AUTHOR_LENGTH_MAX),
        published_at=published_at,
        source_url=source_url,
    )


def _json_entries(body: bytes) -> list[dict[str, object]]:
    try:
        document: object = json.loads(body)
    except (ValueError, RecursionError):
        return []
    if not isinstance(document, dict):
        return []
    feed = cast(dict[str, object], document)
    version = feed.get("version")
    items = feed.get("items")
    if not isinstance(version, str) or not version.startswith("https://jsonfeed.org/version/"):
        return []
    if not isinstance(items, list):
        return []
    entries: list[dict[str, object]] = []
    for item in cast(list[object], items)[:MAX_FEED_ENTRIES]:
        if isinstance(item, dict):
            entries.append(_json_entry(cast(dict[str, object], item)))
    assert len(entries) <= MAX_FEED_ENTRIES
    return entries


def _json_entry(item: dict[str, object]) -> dict[str, object]:
    authors = item.get("authors")
    first = cast(list[object], authors)[0] if isinstance(authors, list) and authors else None
    first = first or item.get("author")
    name = cast(dict[str, object], first).get("name") if isinstance(first, dict) else None
    published = item.get("date_published") or item.get("date_modified")
    identifier = item.get("id")
    return {
        "link": item.get("url"),
        "id": str(identifier) if identifier is not None else None,
        "title": item.get("title"),
        "author": name,
        "published_iso": utc_iso(published) if isinstance(published, str) else None,
    }


def _entries(body: bytes) -> list[Any]:
    if body.lstrip()[:1] == b"{":
        return _json_entries(body)
    try:
        root: Any = parse_xml(body)
    except ValueError:
        return []
    safe: bytes = etree.tostring(root, encoding="utf-8", xml_declaration=True)
    parser: Any = feedparser
    parsed: Any = parser.parse(io.BytesIO(safe), resolve_relative_uris=False, sanitize_html=False)
    return list(parsed.entries[:MAX_FEED_ENTRIES])


def parse_feed(body: bytes, base_url: str) -> list[Candidate] | None:
    """Parse an RSS, Atom or JSON Feed body into candidates, in feed order.

    Returns None when no entry has a usable link, so the body is not a feed. XML bodies
    first pass parse_xml, so any DTD or broken XML fails closed (None), and feedparser only
    sees lxml's re-serialised root as a stream, never bytes or str it could open as a path.
    At most MAX_FEED_ENTRIES are returned.
    """
    assert base_url.startswith(("http://", "https://")), base_url
    found: list[Candidate] = []
    for entry in _entries(body):
        candidate = _candidate(entry, base_url)
        if candidate is not None:
            found.append(candidate)
    assert len(found) <= MAX_FEED_ENTRIES
    return found or None


class _HeadParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.feeds: list[str] = []
        self.title: list[str] = []
        self.in_title = False
        self.title_done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if tag == "title" and not self.title_done:
            self.in_title = True
        rels = values.get("rel", "").lower().split()
        kind = values.get("type", "").lower().strip()
        href = values.get("href", "").strip()
        if tag == "link" and "alternate" in rels and kind in FEED_TYPES and href:
            self.feeds.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self.in_title:
            self.in_title = False
            self.title_done = True

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title.append(data)


def _parse_head(html: bytes) -> _HeadParser:
    parser = _HeadParser()
    parser.feed(decode_html(html))
    parser.close()
    return parser


def feed_links(html: bytes, base_url: str) -> list[str]:
    """Return absolute http(s) URLs of RSS, Atom and JSON Feed alternate links, in order.

    Duplicates are dropped and at most MAX_FEED_LINKS are returned.
    """
    assert base_url.startswith(("http://", "https://")), base_url
    links: list[str] = []
    for href in _parse_head(html).feeds:
        url = urljoin(base_url, href)
        if url.startswith(("http://", "https://")) and url not in links:
            links.append(url)
        if len(links) == MAX_FEED_LINKS:
            break
    assert len(links) == len(set(links))
    return links


def page_title(html: bytes) -> str | None:
    """Return the first <title> text, whitespace collapsed, at most 80 characters."""
    title = _text("".join(_parse_head(html).title))
    if title is None:
        return None
    bounded = title[:PAGE_TITLE_LENGTH_MAX].strip()
    assert 0 < len(bounded) <= PAGE_TITLE_LENGTH_MAX
    return bounded
