import json
from typing import Any, cast

from tests.samples import digest_data, lesson_data
from tests.unit.test_render_lesson import parse, sample_meta

from hackerblog.core.llm.schemas import LessonSpec, PostDigest
from hackerblog.core.render.index import IndexEntry, render_index
from hackerblog.core.render.lesson import render_lesson
from hackerblog.core.render.notes import render_notes

SHORT = "<script>x</script><b onclick=x>"
TINY = "<b onclick=x>"
LONG = (
    "</script><script>alert(1)</script><img src=x onerror=alert(1)>"
    '<a href="javascript:alert(1)">a</a><style>*{}</style><p style="x">p</p>'
    "[link](javascript:alert(1)) <svg onload=alert(1)>"
)
MARKDOWN_KEYS = {"notes_markdown", "body_markdown", "summary", "detail", "explanation"}
KEEP_KEYS = {"type", "icon", "difficulty", "kind", "language"}
LIMITS = {"unit": TINY}


def poison(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        mapping = cast(dict[str, Any], value)
        return {k: v if k in KEEP_KEYS else poison(v, k) for k, v in mapping.items()}
    if isinstance(value, list):
        return [poison(item, key) for item in cast(list[Any], value)]
    if isinstance(value, str):
        return LONG if key in MARKDOWN_KEYS else LIMITS.get(key, SHORT)
    return value


def xss_inputs() -> tuple[PostDigest, LessonSpec]:
    lesson_raw = poison(lesson_data())
    lesson_raw["final_quiz"][1]["steps_in_order"] = [SHORT + str(i) for i in range(4)]
    lesson_raw["final_quiz"][3]["pairs"] = [[SHORT + str(i), SHORT] for i in range(3)]
    return PostDigest.model_validate(poison(digest_data())), LessonSpec.model_validate(lesson_raw)


SAFE_SCRIPT_ATTRS: tuple[dict[str, str], ...] = (
    {"type": "application/json", "id": "lesson-data"},
    {},
)


def assert_safe(html: str, scripts: int) -> None:
    doc = parse(html)
    found = [attrs for tag, attrs in doc.tags if tag == "script"]
    assert len(found) == scripts
    assert all(attrs in SAFE_SCRIPT_ATTRS for attrs in found)
    assert len([tag for tag, _ in doc.tags if tag == "style"]) == 1
    for tag, attrs in doc.tags:
        assert tag not in {"img", "iframe", "object", "embed", "form", "base", "link"}
        assert "style" not in attrs
        assert not any(name.startswith("on") for name in attrs)
        href = attrs.get("href") or ""
        assert not href.lower().startswith(("javascript:", "data:", "vbscript:"))
        assert ":" not in href or href.startswith(("https://", "http://"))


def test_lesson_neutralises_hostile_content() -> None:
    digest, lesson = xss_inputs()
    meta = sample_meta(title=SHORT, author=SHORT, site_name=SHORT)
    html = render_lesson(meta, digest, lesson)
    assert_safe(html, scripts=2)
    data_text = html.split('id="lesson-data">', 1)[1].split("</script>", 1)[0]
    assert "<" not in data_text
    assert json.loads(data_text)["items"]["final-0"]["question"] == SHORT


def test_notes_neutralise_hostile_content() -> None:
    digest, _ = xss_inputs()
    notes = render_notes(sample_meta(title=SHORT), digest, "lesson.html")
    lowered = notes.lower()
    for marker in ("<script", "<img", "<style", "<svg", "<a ", "<p ", "<b ", "javascript:"):
        assert marker not in lowered


def test_index_neutralises_hostile_content() -> None:
    entry = IndexEntry(
        site_slug="alpha",
        site_name=SHORT,
        title=LONG,
        published="2026-01-01",
        lesson_href="alpha/2026-01-01-x/lesson.html",
        notes_href="alpha/2026-01-01-x/notes.md",
        reading_status="unread",
    )
    assert_safe(render_index([entry]), scripts=0)
