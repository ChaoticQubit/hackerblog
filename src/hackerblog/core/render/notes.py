"""Render notes.md: JSON-escaped front matter, then the digest as HTML-free Markdown."""

import json
import re

from hackerblog.core.llm.schemas import PostDigest
from hackerblog.core.render.lesson import PostMeta
from hackerblog.core.render.sanitize import ENV, plain, strip_markdown_html

RELATIVE_FILE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")


def _json(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _front(meta: PostMeta, digest: PostDigest, lesson_rel_path: str) -> dict[str, str]:
    values: dict[str, object] = {
        "title": meta.title,
        "source_url": meta.source_url,
        "site": meta.site_name,
        "published": meta.published_at,
        "processed": meta.generated_at,
        "lesson": lesson_rel_path,
        "tags": list(digest.topics),
    }
    front = {key: _json(value) for key, value in values.items()}
    assert all("\n" not in value for value in front.values())
    return front


def render_notes(meta: PostMeta, digest: PostDigest, lesson_rel_path: str) -> str:
    """Render notes.md for a post; lesson_rel_path is the lesson's path relative to the notes.

    Every text field has raw HTML removed, so Markdown viewers that render HTML stay safe.
    """
    assert RELATIVE_FILE.fullmatch(lesson_rel_path), f"bad lesson path {lesson_rel_path!r}"
    assert ".." not in lesson_rel_path.split("/")
    ideas = [{"claim": plain(i.claim), "detail": plain(i.detail)} for i in digest.key_ideas]
    glossary = [{"term": plain(t.term), "definition": plain(t.definition)} for t in digest.glossary]
    text = ENV.get_template("notes.md.j2").render(
        front=_front(meta, digest, lesson_rel_path),
        title=plain(meta.title),
        lesson=lesson_rel_path,
        source_url=meta.source_url,
        tldr=[plain(line) for line in digest.tldr],
        why=plain(digest.why_it_matters),
        summary=plain(digest.summary),
        ideas=ideas,
        notes=strip_markdown_html(digest.notes_markdown).strip(),
        glossary=glossary,
        gotchas=[plain(gotcha) for gotcha in digest.gotchas],
    )
    assert text.startswith("---\n")
    return text
