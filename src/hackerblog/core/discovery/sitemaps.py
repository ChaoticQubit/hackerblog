"""Sitemap and sitemap index parsing (section 8.3), with the safe XML parser of 8.1."""

from dataclasses import dataclass
from typing import Any

from hackerblog.core.discovery.feeds import parse_xml, utc_iso

__all__ = ["MAX_SITEMAP_ENTRIES", "Sitemap", "local_name", "parse_sitemap", "parse_xml"]

MAX_SITEMAP_ENTRIES = 50000


def local_name(element: Any) -> str:
    """Return an element's tag without its namespace, or "" for comments and PIs."""
    tag: object = element.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


@dataclass(frozen=True)
class Sitemap:
    """One parsed sitemap file: post pages with lastmod, or child sitemaps of an index."""

    pages: list[tuple[str, str | None]]
    children: list[str]


def _fields(entry: Any) -> dict[str, str]:
    found: dict[str, str] = {}
    for child in entry:
        name = local_name(child)
        text: object = child.text
        if name and isinstance(text, str) and name not in found:
            found[name] = text.strip()
    return found


def parse_sitemap(body: bytes) -> Sitemap:
    """Parse a urlset or sitemapindex document.

    pages hold (loc, lastmod as UTC ISO with Z or None); children hold index entries.
    Entries without a loc are skipped and at most MAX_SITEMAP_ENTRIES are kept.
    Raises ValueError for other documents and for unsafe or broken XML.
    """
    root = parse_xml(body)
    kind = local_name(root)
    if kind not in {"urlset", "sitemapindex"}:
        raise ValueError(f"not a sitemap: root element is {kind!r}")
    pages: list[tuple[str, str | None]] = []
    children: list[str] = []
    for entry in root:
        if len(pages) + len(children) == MAX_SITEMAP_ENTRIES:
            break
        fields = _fields(entry)
        loc = fields.get("loc")
        if not loc:
            continue
        if kind == "urlset" and local_name(entry) == "url":
            pages.append((loc, utc_iso(fields.get("lastmod"))))
        if kind == "sitemapindex" and local_name(entry) == "sitemap":
            children.append(loc)
    assert len(pages) + len(children) <= MAX_SITEMAP_ENTRIES
    assert not (pages and children)
    return Sitemap(pages=pages, children=children)
