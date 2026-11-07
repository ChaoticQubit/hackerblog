"""Safety layer of rendering: the Jinja environment, Markdown to safe HTML, Markdown HTML stripping.

Every Markdown field from the LLM or the post is untrusted. HTML output goes through
markdown-it with raw HTML off and then the nh3 allowlist; Markdown output for notes.md has
raw HTML tags removed and every link or image target that is not http(s) or a relative path
replaced by '#', outside code.
"""

import base64
import hashlib
import re
from importlib.resources import files

import mdurl
import nh3
from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape
from markdown_it import MarkdownIt
from markdown_it.helpers import parseLinkDestination

ALLOWED_TAGS = frozenset(
    {
        "p",
        "ul",
        "ol",
        "li",
        "strong",
        "em",
        "code",
        "pre",
        "blockquote",
        "h3",
        "h4",
        "a",
        "hr",
        "br",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
    }
)
LINK_REL = "noopener noreferrer nofollow"
HEADING_MAP = {"h1": "h3", "h2": "h3", "h3": "h4", "h4": "h4", "h5": "h4", "h6": "h4"}
HTML_TAG = re.compile(
    r"<!--.*?-->|<![A-Za-z\[][^>]*>|<\?.*?\?>|</?[A-Za-z][A-Za-z0-9-]*(?:[\s/][^<>]*)?>",
    re.DOTALL,
)
CODE_SPAN = re.compile(r"(`+)(?:(?!\1).)+?\1", re.DOTALL)
DESTINATION = re.compile(r"\]\s*\(\s*|\]:[ \t]*\n?[ \t]*")
AUTOLINK = re.compile(r"<([A-Za-z][A-Za-z0-9+.-]{1,31}:[^<>\x00-\x20]*|[^<>\s@]+@[^<>\s]+)>")
SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*):")
SAFE_SCHEMES = frozenset({"http", "https"})
URL_STRIPPED = "".join(chr(code) for code in range(0x21))
UNSAFE_SCHEME = re.compile(r"(?:javascript|vbscript)\s*:", re.IGNORECASE)
ASSETS = files("hackerblog.core.render") / "templates" / "assets"

_markdown = MarkdownIt("commonmark", {"html": False})
_links = MarkdownIt("commonmark", {"html": False})
_links.validateLink = lambda url: True


ENV = Environment(
    loader=PackageLoader("hackerblog.core.render"),
    autoescape=select_autoescape(
        enabled_extensions=("html.j2",),
        disabled_extensions=("md.j2",),
        default_for_string=True,
        default=True,
    ),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)


def asset(name: str) -> str:
    """Return the text of a packaged asset under templates/assets."""
    text = (ASSETS / name).read_text(encoding="utf-8")
    assert text, f"empty asset {name}"
    return text


def csp_hash(text: str) -> str:
    """Return the CSP source expression 'sha256-...' for an inline script or style body."""
    digest = base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii")
    assert len(digest) == 44
    return f"'sha256-{digest}'"


def markdown_html(text: str) -> str:
    """Render untrusted Markdown to HTML limited to the allowlist, with safe links only.

    Raw HTML in the input is shown as text. Headings are shifted to h3 and h4. Templates
    may mark the result safe because it has passed nh3.
    """
    tokens = _markdown.parse(text)
    for token in tokens:
        if token.type in {"heading_open", "heading_close"}:
            token.tag = HEADING_MAP[token.tag]
    rendered = _markdown.renderer.render(tokens, _markdown.options, {})
    cleaned = nh3.clean(
        rendered,
        tags=set(ALLOWED_TAGS),
        attributes={"a": {"href"}},
        url_schemes={"http", "https"},
        link_rel=LINK_REL,
        url_relative=lambda url: url if url.startswith("#") else None,
    )
    assert "<script" not in cleaned.lower()
    assert re.search(r"<[^>]*\sstyle=", cleaned) is None
    return cleaned


def safe_href(href: str) -> bool:
    """True if a link target is http(s) or a relative path, as a browser would read it.

    Percent escapes are decoded and tab, newline and edge control characters removed first;
    protocol-relative targets count as unsafe.
    """
    raw = re.sub(r"[\t\n\r]", "", mdurl.decode(href)).strip(URL_STRIPPED)
    if raw.replace("\\", "/").startswith("//"):
        return False
    scheme = SCHEME.match(raw)
    return scheme is None or scheme.group(1).lower() in SAFE_SCHEMES


def _unsafe_targets(markdown: str) -> list[str]:
    found: list[str] = []
    for block in _links.parse(markdown):
        for token in block.children or []:
            target = token.attrs.get("href") if token.type == "link_open" else None
            target = token.attrs.get("src") if token.type == "image" else target
            if target is not None and not safe_href(str(target)):
                found.append(str(target))
    return found


def _drop_targets(text: str) -> str:
    out: list[str] = []
    last = 0
    for match in DESTINATION.finditer(text):
        if match.end() < last:
            continue
        dest = parseLinkDestination(text, match.end(), len(text))
        if dest.ok and not safe_href(_links.normalizeLink(dest.str)):
            out.extend((text[last : match.end()], "#"))
            last = dest.pos
    out.append(text[last:])
    dropped = "".join(out)

    def autolink(match: re.Match[str]) -> str:
        inner = match.group(1)
        keep = SCHEME.match(inner) is not None and safe_href(inner)
        return match.group(0) if keep else inner

    return AUTOLINK.sub(autolink, dropped)


def _escape_links(text: str) -> str:
    return re.sub(r"([\[\]<])", r"\\\1", text)


def _clean_prose(text: str, escape: bool) -> str:
    stripped = _drop_targets(HTML_TAG.sub("", text))
    return UNSAFE_SCHEME.sub("", _escape_links(stripped) if escape else stripped)


def _strip_inline(text: str, escape: bool) -> str:
    pieces: list[str] = []
    last = 0
    for match in CODE_SPAN.finditer(text):
        pieces.append(_clean_prose(text[last : match.start()], escape))
        pieces.append(match.group(0))
        last = match.end()
    pieces.append(_clean_prose(text[last:], escape))
    assert len(pieces) % 2 == 1
    return "".join(pieces)


def clean_markdown(text: str, escape: bool) -> str:
    """Strip raw HTML and unsafe link targets outside code; escape every link bracket if escape."""
    lines = text.splitlines(keepends=True)
    code_lines: set[int] = set()
    for token in _markdown.parse(text):
        if token.type in {"fence", "code_block"} and token.map is not None:
            code_lines.update(range(token.map[0], token.map[1]))
    out: list[str] = []
    chunk: list[str] = []
    for number, line in enumerate(lines):
        if number in code_lines:
            out.append(_strip_inline("".join(chunk), escape))
            chunk = []
            out.append(line)
        else:
            chunk.append(line)
    out.append(_strip_inline("".join(chunk), escape))
    result = "".join(out)
    assert all(lines[number] in result for number in code_lines)
    return result


def strip_markdown_html(text: str) -> str:
    """Remove raw HTML tags and comments from Markdown and replace unsafe link targets by '#'.

    A target is unsafe unless it is http(s) or a relative path. Fenced and indented code
    blocks and inline code spans are kept verbatim. If markdown-it still finds an unsafe
    target, every link bracket outside code is escaped instead.
    """
    result = clean_markdown(text, escape=False)
    if _unsafe_targets(result):
        result = clean_markdown(text, escape=True)
    assert not _unsafe_targets(result), "unsafe link target survived"
    return result


def plain(text: str) -> str:
    """Return one-line plain text for Markdown output.

    HTML tags and script schemes are removed and `[`, `]` and `<` are backslash-escaped, so no
    link, image or autolink can form whatever its target.
    """
    cleaned = UNSAFE_SCHEME.sub("", _escape_links(HTML_TAG.sub("", text)))
    return " ".join(cleaned.split())
