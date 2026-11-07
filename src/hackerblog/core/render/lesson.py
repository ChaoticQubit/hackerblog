"""Render the single-file interactive lesson.html and post.json for one processed post."""

import hashlib
import json
import math
import re
import textwrap
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from itertools import pairwise
from typing import Any

from hackerblog.core.llm.schemas import Diagram, LessonSpec, PostDigest
from hackerblog.core.render.sanitize import ENV, asset, csp_hash, markdown_html

TEMPLATE_VERSION = "1"
POST_JSON_SCHEMA = 1
LESSON_JS = asset("lesson.js")
LESSON_CSS = asset("lesson.css")
ICONS = asset("icons.svg")
LESSON_CSP = (
    f"default-src 'none'; script-src {csp_hash(LESSON_JS)}; style-src {csp_hash(LESSON_CSS)}; "
    "img-src data:; font-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'"
)
FLAG_MESSAGES = {
    "possible_injection": "This post contains text that looks like instructions to an AI. "
    "The lesson was made without tools or network access, but read it with care and check "
    "the original.",
    "quiz_ungrounded": "The final quiz was left out because its questions could not be "
    "matched to the post.",
    "quiz_low_variety": "The final quiz uses fewer question types than usual, so it may "
    "test the same skill several times.",
}
GAME_HEADINGS = {
    "ordering": "Put the steps in order",
    "spot_the_bug": "Spot the bug",
    "match_pairs": "Match the pairs",
}
PAD = 8
UNSAFE_URL_CHAR = re.compile(r'[\s<>"\\\x00-\x1f\x7f-\x9f]')

ENV.filters["md"] = markdown_html


@dataclass(frozen=True)
class PostMeta:
    """Post facts the templates show: identity, source, reading time, flags and provenance."""

    post_id: int
    title: str
    site_slug: str
    site_name: str
    author: str | None
    published_at: str | None
    source_url: str
    reading_minutes: int
    flags: tuple[str, ...]
    truncated: bool
    models: tuple[str, ...]
    prompt_versions: Mapping[str, str] = field(hash=False)
    generated_at: str

    def __post_init__(self) -> None:
        if not self.source_url.startswith(("https://", "http://")):
            raise ValueError(f"source_url must be http(s): {self.source_url!r}")
        if UNSAFE_URL_CHAR.search(self.source_url):
            raise ValueError(f"source_url has a forbidden character: {self.source_url!r}")
        assert self.post_id > 0 and self.reading_minutes >= 0
        assert self.title


def _wrap(label: str, width: int) -> list[str]:
    lines = textwrap.wrap(label, width=width, break_long_words=True) or [""]
    if len(lines) > 2:
        lines = [lines[0], textwrap.shorten(" ".join(lines[1:]), width=width, placeholder="…")]
    return lines


def _node(box: tuple[float, float, float, float], label: str, width: int) -> dict[str, Any]:
    x, y, w, h = box
    lines = _wrap(label, width)
    first = y + h / 2 - (len(lines) - 1) * 9 + 5
    texts = [{"text": t, "x": x + w / 2, "y": first + i * 18} for i, t in enumerate(lines)]
    return {"x": x, "y": y, "w": w, "h": h, "lines": texts}


def _flow(diagram: Diagram) -> dict[str, Any]:
    count, w, h, gx, gy = len(diagram.nodes), 180, 72, 48, 56
    cols = min(count, 4)
    rows = math.ceil(count / cols)
    nodes = [
        _node((PAD + (i % cols) * (w + gx), PAD + (i // cols) * (h + gy), w, h), n.label, 20)
        for i, n in enumerate(diagram.nodes)
    ]
    edges: list[str] = []
    for a, b in pairwise(nodes):
        if a["y"] == b["y"]:
            edges.append(f"M{a['x'] + w} {a['y'] + h / 2}H{b['x'] - 4}")
        else:
            mid = a["y"] + h + gy / 2
            edges.append(f"M{a['x'] + w / 2} {a['y'] + h}V{mid}H{b['x'] + w / 2}V{b['y'] - 4}")
    width = 2 * PAD + cols * w + (cols - 1) * gx
    height = 2 * PAD + rows * h + (rows - 1) * gy
    return {"nodes": nodes, "edges": edges, "dots": [], "width": width, "height": height}


def _layers(diagram: Diagram) -> dict[str, Any]:
    w, h, gap = 640, 52, 10
    nodes = [
        _node((PAD, PAD + i * (h + gap), w, h), n.label, 60) for i, n in enumerate(diagram.nodes)
    ]
    height = 2 * PAD + len(nodes) * h + (len(nodes) - 1) * gap
    return {"nodes": nodes, "edges": [], "dots": [], "width": 2 * PAD + w, "height": height}


def _timeline(diagram: Diagram) -> dict[str, Any]:
    step, line_y = 140, 24
    width = 2 * PAD + len(diagram.nodes) * step
    nodes = [
        _node((PAD + i * step + 4, 44, step - 8, 48), n.label, 16)
        for i, n in enumerate(diagram.nodes)
    ]
    dots = [{"cx": PAD + i * step + step / 2, "cy": line_y} for i in range(len(nodes))]
    edges = [f"M{PAD} {line_y}H{width - PAD}"]
    return {"nodes": nodes, "edges": edges, "dots": dots, "width": width, "height": 100}


def diagram_shapes(diagram: Diagram) -> dict[str, Any]:
    """Lay out a diagram as boxes, dots, edge paths and wrapped label lines in SVG units."""
    layout = {"flow": _flow, "layers": _layers, "timeline": _timeline}[diagram.kind]
    shapes = layout(diagram)
    assert len(shapes["nodes"]) == len(diagram.nodes)
    assert shapes["width"] > 0 and shapes["height"] > 0
    return shapes


def _shuffle(key: str, count: int) -> list[int]:
    order = sorted(range(count), key=lambda i: hashlib.sha256(f"{key}:{i}".encode()).digest())
    if order == list(range(count)):
        order = order[1:] + order[:1]
    assert sorted(order) == list(range(count))
    return order


def _item(item: Any, key: str) -> dict[str, Any]:
    data: dict[str, Any] = item.model_dump(mode="json")
    if data["type"] == "ordering":
        data["shuffle"] = _shuffle(key, len(data["steps_in_order"]))
    if data["type"] == "match_pairs":
        data["shuffle"] = _shuffle(key, len(data["pairs"]))
    return data


def lesson_data(meta: PostMeta, digest: PostDigest, lesson: LessonSpec) -> dict[str, Any]:
    """Return the JSON data island the page script reads: quiz items, game, slider, cards."""
    items: dict[str, Any] = {}
    for index, section in enumerate(lesson.sections):
        if section.check is not None:
            items[f"check-{index}"] = _item(section.check, f"{meta.post_id}:check-{index}")
    if lesson.game is not None:
        items["game"] = _item(lesson.game, f"{meta.post_id}:game")
    final = [f"final-{i}" for i in range(len(lesson.final_quiz))]
    for key, item in zip(final, lesson.final_quiz, strict=True):
        items[key] = _item(item, f"{meta.post_id}:{key}")
    explorable = lesson.explorable.model_dump(mode="json") if lesson.explorable else None
    data = {
        "post_id": meta.post_id,
        "storage_key": f"hackerblog:lesson:{meta.post_id}",
        "items": items,
        "final": final,
        "explorable": explorable,
        "flashcards": [term.model_dump(mode="json") for term in digest.glossary],
    }
    assert all(key in items for key in final)
    return data


def render_lesson(meta: PostMeta, digest: PostDigest, lesson: LessonSpec) -> str:
    """Render the self-contained, offline, CSP-pinned lesson.html for one post."""
    questions = len(lesson.final_quiz) + sum(1 for s in lesson.sections if s.check is not None)
    page = ENV.get_template("lesson.html.j2").render(
        meta=meta,
        digest=digest,
        lesson=lesson,
        csp=LESSON_CSP,
        css=LESSON_CSS,
        js=LESSON_JS,
        icons=ICONS,
        data=lesson_data(meta, digest, lesson),
        shapes=diagram_shapes(lesson.diagram) if lesson.diagram else None,
        notices=[FLAG_MESSAGES.get(flag, f"Flag: {flag}") for flag in meta.flags],
        game_heading=GAME_HEADINGS.get(lesson.game.type, "Challenge") if lesson.game else "",
        questions=questions,
        template_version=TEMPLATE_VERSION,
    )
    assert page.count("<script") == 2
    assert f"<style>{LESSON_CSS}</style>" in page
    return page


def post_json(
    meta: PostMeta, digest: PostDigest, lesson: LessonSpec, generation: Mapping[str, Any]
) -> str:
    """Return post.json (section 13): metadata, digest, lesson and generation details."""
    assert set(generation) <= {"models", "prompt_versions", "tokens"}
    post = asdict(meta) | {"template_version": TEMPLATE_VERSION}
    post["prompt_versions"] = dict(meta.prompt_versions)
    document = {
        "schema_version": POST_JSON_SCHEMA,
        "post": post,
        "digest": digest.model_dump(mode="json"),
        "lesson": lesson.model_dump(mode="json"),
        "generation": dict(generation),
    }
    text = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    assert json.loads(text)["post"]["post_id"] == meta.post_id
    return text
