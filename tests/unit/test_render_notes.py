import json

from tests.samples import digest_data
from tests.unit.test_render_lesson import sample_meta

from hackerblog.core.llm.schemas import PostDigest
from hackerblog.core.render.notes import render_notes


def front_matter(text: str) -> dict[str, object]:
    assert text.startswith("---\n")
    block = text[4 : text.index("\n---\n", 4)]
    pairs = (line.split(": ", 1) for line in block.splitlines())
    return {key: json.loads(value) for key, value in pairs}


def test_front_matter_values_are_json() -> None:
    digest = PostDigest.model_validate(digest_data())
    notes = render_notes(sample_meta(title='Quote " and: colon\nnewline'), digest, "lesson.html")
    meta = front_matter(notes)
    assert meta == {
        "title": 'Quote " and: colon\nnewline',
        "source_url": "https://blog.example.com/posts/limit-overrun-races",
        "site": "Example Security Blog",
        "published": "2026-10-28T09:00:00Z",
        "processed": "2026-11-06T12:00:00Z",
        "lesson": "lesson.html",
        "tags": ["race conditions", "http2"],
    }


def test_body_sections_in_order() -> None:
    digest = PostDigest.model_validate(digest_data())
    notes = render_notes(sample_meta(), digest, "lesson.html")
    order = ["## TL;DR", "## Summary", "## Key ideas", "## Notes", "## Glossary", "## Gotchas"]
    positions = [notes.index(heading) for heading in order]
    assert positions == sorted(positions)
    assert "UPDATE coupons SET uses = uses - 1 WHERE uses > 0" in notes
    assert "[Open the lesson](lesson.html)" in notes


def test_notes_strip_raw_html() -> None:
    raw = digest_data()
    raw["notes_markdown"] = "Hi <script>alert(1)</script>\n\n<iframe src=x></iframe>\n"
    raw["summary"] = "Sum <img src=x onerror=alert(1)>"
    notes = render_notes(sample_meta(), PostDigest.model_validate(raw), "lesson.html")
    assert "<script" not in notes and "<iframe" not in notes and "<img" not in notes
