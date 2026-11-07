"""Full-text search over ready posts through the search_index FTS5 table (section 10)."""

import sqlite3
from dataclasses import dataclass

SEARCH_LIMIT_MAX = 100
QUERY_CHARS_MAX = 500
QUERY_TOKENS_MAX = 32
MARK_OPEN = "["
MARK_CLOSE = "]"

SEARCH_SQL = """
SELECT posts.id, sites.slug, coalesce(posts.title, search_index.title, ''),
       snippet(search_index, -1, ?, ?, '...', 12)
FROM search_index
JOIN posts ON posts.id = CAST(search_index.post_id AS INTEGER)
JOIN sites ON sites.id = posts.site_id
WHERE search_index MATCH ? AND posts.status = 'ready' AND (? IS NULL OR sites.slug = ?)
ORDER BY bm25(search_index, 10.0, 5.0, 3.0, 1.0), posts.id
LIMIT ?
"""


@dataclass(frozen=True)
class SearchHit:
    """One ranked match: post id, site slug, title and a snippet with the match in brackets."""

    post_id: int
    site: str
    title: str
    snippet: str


def fts_query(text: str) -> str:
    """Turn user text into FTS5 terms that can never be query syntax.

    Each whitespace-separated token is wrapped in double quotes with inner quotes doubled,
    so operators, column filters, NEAR and * stay literal. Tokens without a letter or digit
    are dropped, since they would be empty phrases. Returns "" when nothing searchable is left.
    """
    tokens = [token for token in text.split() if any(char.isalnum() for char in token)]
    quoted = ['"' + token.replace('"', '""') + '"' for token in tokens[:QUERY_TOKENS_MAX]]
    query = " ".join(quoted)
    assert query.count('"') % 2 == 0
    assert len(quoted) <= QUERY_TOKENS_MAX
    return query


def search(
    conn: sqlite3.Connection, text: str, site: str | None = None, limit: int = 10
) -> list[SearchHit]:
    """Rank posts matching every token of text by bm25, titles weighted highest.

    site limits hits to one site slug. limit is 1..100. Raises ValueError when text is empty
    or longer than 500 characters. Returns at most limit hits, best first.
    """
    assert 1 <= limit <= SEARCH_LIMIT_MAX, limit
    if not text.strip():
        raise ValueError("search text is empty")
    if len(text) > QUERY_CHARS_MAX:
        raise ValueError(f"search text is longer than {QUERY_CHARS_MAX} characters")
    query = fts_query(text)
    if not query:
        return []
    params = (MARK_OPEN, MARK_CLOSE, query, site, site, limit)
    rows = conn.execute(SEARCH_SQL, params).fetchall()
    hits = [SearchHit(int(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in rows]
    assert len(hits) <= limit
    assert site is None or all(hit.site == site for hit in hits)
    return hits
