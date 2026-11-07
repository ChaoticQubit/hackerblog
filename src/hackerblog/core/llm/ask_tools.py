"""Read-only tools for `ask` mode (section 11.5 item 6).

build_tools returns plain typed, documented functions an agent framework can expose as tools.
They only read, never touch the network or files, return JSON strings with control characters
stripped, and cap post bodies at 4000 characters. Arguments come from a model, so each tool
checks and clamps them instead of trusting them.
"""

import json
import re
import sqlite3
from collections.abc import Callable
from typing import Any, cast

from hackerblog.core.store.search import QUERY_CHARS_MAX, SEARCH_LIMIT_MAX, search

BODY_CHARS_MAX = 4000
TOOL_LIMIT_MAX = 20
DIGEST_FIELDS = ("tldr", "why_it_matters", "summary", "key_ideas", "notes_markdown", "topics")
CONTROL = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]"
)

AskTools = tuple[
    Callable[[str, int], str],
    Callable[[int], str],
    Callable[[str | None, int], str],
]

RECENT_SQL = """
SELECT posts.id, sites.slug, coalesce(posts.title, ''), posts.published_at, posts.reading_status
FROM posts JOIN sites ON sites.id = posts.site_id
WHERE posts.status = 'ready' AND (? IS NULL OR sites.slug = ?)
ORDER BY coalesce(posts.published_at, posts.discovered_at) DESC, posts.id DESC
LIMIT ?
"""

POST_SQL = """
SELECT posts.id, sites.slug, coalesce(posts.title, ''), posts.url, posts.published_at,
       posts.digest_json, coalesce(posts.body_md, '')
FROM posts JOIN sites ON sites.id = posts.site_id
WHERE posts.id = ? AND posts.status = 'ready' AND (? IS NULL OR sites.slug = ?)
"""


def _strip(value: Any) -> Any:
    if isinstance(value, str):
        return CONTROL.sub("", value)
    if isinstance(value, list):
        return [_strip(item) for item in cast(list[Any], value)]
    if isinstance(value, dict):
        return {str(key): _strip(item) for key, item in cast(dict[Any, Any], value).items()}
    return value


def _dump(value: object) -> str:
    text = json.dumps(_strip(value), ensure_ascii=False)
    assert CONTROL.search(text) is None
    return text


def _clamp(limit: object, default: int) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool):
        return default
    return max(1, min(limit, TOOL_LIMIT_MAX))


def _post(conn: sqlite3.Connection, site: str | None, post_id: object) -> str:
    if not isinstance(post_id, int) or isinstance(post_id, bool):
        return _dump({"error": "post_id must be an integer"})
    row = conn.execute(POST_SQL, (post_id, site, site)).fetchone()
    if row is None:
        return _dump({"error": f"post {post_id} not found"})
    digest: dict[str, Any] = json.loads(row[5]) if row[5] else {}
    body = str(row[6])
    post: dict[str, object] = {
        "id": row[0],
        "site": row[1],
        "title": row[2],
        "url": row[3],
        "published_at": row[4],
    }
    post.update({field: digest.get(field) for field in DIGEST_FIELDS})
    post["body_md"] = body[:BODY_CHARS_MAX]
    post["body_truncated"] = len(body) > BODY_CHARS_MAX
    assert len(str(post["body_md"])) <= BODY_CHARS_MAX
    assert post["id"] == post_id
    return _dump(post)


def build_tools(conn: sqlite3.Connection, site: str | None = None) -> AskTools:
    """Return (search_notes, get_post, list_recent) bound to conn, optionally to one site.

    With site set, every tool only sees that site's posts, whatever the model asks for.
    Sets PRAGMA query_only on conn, so neither the tools nor the caller can write through it.
    """
    conn.execute("PRAGMA query_only = ON")
    assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
    bound = site

    def search_notes(query: str, limit: int = 5) -> str:
        """Full-text search over post titles, summaries, notes and text.

        Args: query is plain words; limit is how many hits to return (1-20).
        Returns a JSON list of {post_id, site, title, snippet}, best match first.
        """
        text = str(query)[:QUERY_CHARS_MAX]
        if not text.strip():
            return _dump([])
        hits = search(conn, text, bound, min(_clamp(limit, 5), SEARCH_LIMIT_MAX))
        return _dump([hit.__dict__ for hit in hits])

    def get_post(post_id: int) -> str:
        """Fetch one ready post: title, URL, digest fields, notes and up to 4000 characters
        of its text.

        Args: post_id is the id from search_notes or list_recent.
        Returns a JSON object, or {"error": ...} when the post is unknown.
        """
        return _post(conn, bound, post_id)

    def list_recent(site: str | None = None, limit: int = 10) -> str:
        """List the newest ready posts.

        Args: site limits to one site, or null for all; limit is 1-20.
        Returns a JSON list of {id, site, title, published_at, reading_status}, newest first.
        """
        asked = site if isinstance(site, str) else None
        if bound is not None and asked not in {None, bound}:
            return _dump([])
        scope = bound if bound is not None else asked
        rows = conn.execute(RECENT_SQL, (scope, scope, _clamp(limit, 10))).fetchall()
        keys = ("id", "site", "title", "published_at", "reading_status")
        return _dump([dict(zip(keys, row, strict=True)) for row in rows])

    tools: AskTools = (search_notes, get_post, list_recent)
    assert len(tools) == 3
    assert all(tool.__doc__ for tool in (search_notes, get_post, list_recent))
    return tools
