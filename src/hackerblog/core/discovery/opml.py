"""OPML subscription list parsing for `sites import-opml` (section 7.3)."""

from dataclasses import dataclass
from typing import Any

from hackerblog.core.discovery.sitemaps import local_name, parse_xml

MAX_OPML_OUTLINES = 10000


@dataclass(frozen=True)
class OpmlEntry:
    """One followable outline: url is htmlUrl (or xmlUrl when missing), feed is xmlUrl."""

    url: str
    feed: str | None
    title: str | None


def _http(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped.startswith(("http://", "https://")) else None


def _attributes(outline: Any) -> dict[str, str]:
    pairs: list[tuple[object, object]] = list(outline.attrib.items())
    return {str(key).lower(): str(value) for key, value in pairs}


def _entry(attributes: dict[str, str]) -> OpmlEntry | str | None:
    html_url = attributes.get("htmlurl")
    xml_url = attributes.get("xmlurl")
    if html_url is None and xml_url is None:
        return None
    title = (attributes.get("title") or attributes.get("text") or "").strip() or None
    feed = _http(xml_url)
    url = _http(html_url) or feed
    if url is None:
        return f"outline {title!r}: no http(s) URL (htmlUrl={html_url!r}, xmlUrl={xml_url!r})"
    return OpmlEntry(url=url, feed=feed, title=title)


def parse_opml(body: bytes) -> tuple[list[OpmlEntry], list[str]]:
    """Parse an OPML document into followable entries and descriptions of invalid outlines.

    Nested outlines are included in document order; folder outlines without htmlUrl or
    xmlUrl are skipped. Raises ValueError when the body is not safe, well-formed OPML.
    At most MAX_OPML_OUTLINES outlines are read.
    """
    root = parse_xml(body)
    if local_name(root) != "opml":
        raise ValueError(f"not OPML: root element is {local_name(root)!r}")
    entries: list[OpmlEntry] = []
    invalid: list[str] = []
    for index, outline in enumerate(root.iter("{*}outline")):
        if index == MAX_OPML_OUTLINES:
            break
        result = _entry(_attributes(outline))
        if isinstance(result, OpmlEntry):
            entries.append(result)
        elif result is not None:
            invalid.append(result)
    assert len(entries) + len(invalid) <= MAX_OPML_OUTLINES
    assert all(entry.url.startswith(("http://", "https://")) for entry in entries)
    return entries, invalid
