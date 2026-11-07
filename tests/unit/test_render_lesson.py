import base64
import hashlib
import json
import re
from html.parser import HTMLParser
from importlib.resources import files
from typing import Any, get_args

import pytest
from tests.samples import digest_data, lesson_data

from hackerblog.core.llm.schemas import Icon, LessonSpec, PostDigest
from hackerblog.core.render.lesson import (
    TEMPLATE_VERSION,
    PostMeta,
    diagram_shapes,
    post_json,
    render_lesson,
)

ASSETS = files("hackerblog.core.render") / "templates" / "assets"


def sample_meta(**changes: Any) -> PostMeta:
    base: dict[str, Any] = {
        "post_id": 7,
        "title": "Limit overrun races in web apps",
        "site_slug": "example-blog",
        "site_name": "Example Security Blog",
        "author": "Sam Rivera",
        "published_at": "2026-10-28T09:00:00Z",
        "source_url": "https://blog.example.com/posts/limit-overrun-races",
        "reading_minutes": 4,
        "flags": (),
        "truncated": False,
        "models": ("anthropic:claude-sonnet-5-5",),
        "prompt_versions": {"digest": "d1", "lesson": "l1"},
        "generated_at": "2026-11-06T12:00:00Z",
    }
    base.update(changes)
    return PostMeta(**base)


class Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.text: dict[str, list[str]] = {}
        self._open: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))
        self._open = tag if tag in {"script", "style"} else None

    def handle_data(self, data: str) -> None:
        if self._open is not None:
            self.text.setdefault(self._open, []).append(data)

    def handle_endtag(self, tag: str) -> None:
        self._open = None


def parse(html: str) -> Collector:
    collector = Collector()
    collector.feed(html)
    return collector


def sha(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()


@pytest.fixture(scope="module")
def page() -> str:
    digest = PostDigest.model_validate(digest_data())
    lesson = LessonSpec.model_validate(lesson_data())
    return render_lesson(sample_meta(), digest, lesson)


def test_page_has_core_sections(page: str) -> None:
    for heading in ("TL;DR", "Key ideas", "Final quiz", "Flashcards", "Glossary", "Ask the agent"):
        assert heading in page
    assert "Limit overrun races in web apps" in page
    assert "Read the original" in page
    assert "hackerblog ask --post 7" in page


def test_csp_hashes_match_inlined_assets(page: str) -> None:
    doc = parse(page)
    csp = next(a["content"] for t, a in doc.tags if t == "meta" and a.get("http-equiv"))
    assert csp is not None
    assert "default-src 'none'" in csp and "connect-src 'none'" in csp
    assert "base-uri 'none'" in csp and "form-action 'none'" in csp
    script_hash = re.search(r"script-src 'sha256-([^']+)'", csp)
    style_hash = re.search(r"style-src 'sha256-([^']+)'", csp)
    assert script_hash and style_hash
    inline_js = "".join(doc.text["script"][1:])
    assert script_hash.group(1) == sha(inline_js) == sha((ASSETS / "lesson.js").read_text())
    assert style_hash.group(1) == sha("".join(doc.text["style"]))
    assert style_hash.group(1) == sha((ASSETS / "lesson.css").read_text())


def test_data_island_parses(page: str) -> None:
    doc = parse(page)
    scripts = [a for t, a in doc.tags if t == "script"]
    assert scripts[0] == {"type": "application/json", "id": "lesson-data"}
    data = json.loads(doc.text["script"][0])
    assert data["post_id"] == 7
    assert data["final"] == ["final-0", "final-1", "final-2", "final-3"]
    assert data["items"]["game"]["type"] == "spot_the_bug"
    assert data["items"]["check-0"]["type"] == "short_answer"
    assert len(data["flashcards"]) == 2


def test_no_style_attributes_or_handlers(page: str) -> None:
    for _, attrs in parse(page).tags:
        assert "style" not in attrs
        assert not any(name.startswith("on") for name in attrs)


def test_every_icon_has_a_symbol() -> None:
    sprite = (ASSETS / "icons.svg").read_text()
    for icon in get_args(Icon):
        assert f'id="i-{icon}"' in sprite


def test_lesson_js_never_uses_dangerous_sinks() -> None:
    js = (ASSETS / "lesson.js").read_text()
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function"):
        assert sink not in js
    assert "document.write" not in js
    assert 'setAttribute("on' not in js


def test_ungrounded_quiz_renders_without_final_quiz() -> None:
    digest = PostDigest.model_validate(digest_data())
    lesson = LessonSpec.model_validate(lesson_data()).model_copy(update={"final_quiz": []})
    page = render_lesson(sample_meta(flags=("quiz_ungrounded",)), digest, lesson)
    assert "Final quiz" not in page
    assert "could not be matched to the post" in page
    data = json.loads(parse(page).text["script"][0])
    assert data["final"] == []


def test_flags_and_truncation_banners() -> None:
    digest = PostDigest.model_validate(digest_data())
    lesson = LessonSpec.model_validate(lesson_data())
    page = render_lesson(sample_meta(flags=("possible_injection",), truncated=True), digest, lesson)
    assert "Based on the first part of a long post" in page
    assert "looks like instructions to an AI" in page


def test_lesson_without_optional_parts() -> None:
    digest = PostDigest.model_validate(digest_data())
    lesson = LessonSpec.model_validate(lesson_data()).model_copy(
        update={"diagram": None, "explorable": None, "game": None}
    )
    page = render_lesson(sample_meta(author=None, published_at=None), digest, lesson)
    assert '<svg class="diagram' not in page
    data = json.loads(parse(page).text["script"][0])
    assert data["explorable"] is None
    assert "game" not in data["items"]


@pytest.mark.parametrize("kind", ["flow", "layers", "timeline"])
def test_diagram_shapes_fit_viewbox(kind: str) -> None:
    raw = lesson_data()["diagram"]
    raw["kind"] = kind
    raw["nodes"] = [
        {"label": f"Step number {i} with a long label", "detail": "d"} for i in range(8)
    ]
    lesson = LessonSpec.model_validate({**lesson_data(), "diagram": raw})
    assert lesson.diagram is not None
    shapes = diagram_shapes(lesson.diagram)
    assert len(shapes["nodes"]) == 8
    for node in shapes["nodes"]:
        assert node["x"] >= 0 and node["x"] + node["w"] <= shapes["width"]
        assert node["y"] >= 0 and node["y"] + node["h"] <= shapes["height"]
        assert 1 <= len(node["lines"]) <= 2


def test_post_json_shape() -> None:
    digest = PostDigest.model_validate(digest_data())
    lesson = LessonSpec.model_validate(lesson_data())
    generation = {"models": {"digest": "m"}, "prompt_versions": {"digest": "d1"}, "tokens": {}}
    doc = json.loads(post_json(sample_meta(), digest, lesson, generation))
    assert doc["schema_version"] == 1
    assert doc["post"]["post_id"] == 7
    assert doc["post"]["template_version"] == TEMPLATE_VERSION
    assert PostDigest.model_validate(doc["digest"]) == digest
    assert LessonSpec.model_validate(doc["lesson"]) == lesson
    assert doc["generation"] == generation


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://a.example/x> [y](javascript:alert(1))",
        "https://a.example/a b",
        "https://a.example/<script>",
        'https://a.example/" onmouseover="x',
        "https://a.example/\\evil",
        "https://a.example/\x00",
        "https://a.example/\n# injected",
        "https://a.example/\x7f",
    ],
)
def test_meta_rejects_hostile_source_url(url: str) -> None:
    with pytest.raises(ValueError, match="source_url"):
        sample_meta(source_url=url)


def test_ask_command_quotes_with_single_quotes() -> None:
    script = (ASSETS / "lesson.js").read_text(encoding="utf-8")
    assert "const shellQuote = (text) => `'${text.replace(/'/g, \"'\\\\''\")}'`;" in script
