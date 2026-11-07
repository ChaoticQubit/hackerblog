import copy
import json
from typing import Any

import pytest
from pydantic import ValidationError
from tests.samples import digest_data, lesson_data

from hackerblog.core.llm.schemas import (
    FINAL_QUIZ_MIN,
    Generated,
    LessonSpec,
    PostDigest,
    lesson_problems,
    quiz_item_problems,
    quiz_variety_problems,
)


def test_sample_digest_and_lesson_are_valid() -> None:
    PostDigest.model_validate(digest_data())
    lesson = LessonSpec.model_validate(lesson_data())
    assert lesson_problems(lesson) == []


def test_extra_keys_are_rejected() -> None:
    data = digest_data()
    data["html"] = "<script>alert(1)</script>"
    with pytest.raises(ValidationError, match="extra"):
        PostDigest.model_validate(data)


def test_bounds_are_enforced() -> None:
    data = digest_data()
    data["title"] = "x" * 201
    with pytest.raises(ValidationError):
        PostDigest.model_validate(data)
    data = digest_data()
    data["tldr"] = ["a", "b"]
    with pytest.raises(ValidationError):
        PostDigest.model_validate(data)


def test_unknown_icon_and_language_pattern_are_rejected() -> None:
    data = digest_data()
    data["key_ideas"][0]["icon"] = "javascript:alert(1)"
    with pytest.raises(ValidationError):
        PostDigest.model_validate(data)
    data = digest_data()
    data["snippets"][0]["language"] = 'py" onload="x'
    with pytest.raises(ValidationError):
        PostDigest.model_validate(data)


def test_quiz_discriminator_picks_the_right_type() -> None:
    lesson = LessonSpec.model_validate(lesson_data())
    assert {item.type for item in lesson.final_quiz} >= {"mcq", "ordering", "short_answer"}


def _problems(mutate: Any) -> list[str]:
    data = copy.deepcopy(lesson_data())
    mutate(data)
    return lesson_problems(LessonSpec.model_validate(data))


def test_correct_index_beyond_options_is_a_problem() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["final_quiz"][0]["correct_index"] = 4

    assert any("correct_index" in p for p in _problems(mutate))


def test_bug_line_beyond_lines_is_an_item_problem() -> None:
    data = lesson_data()
    data["game"]["bug_line"] = 20
    game = LessonSpec.model_validate(data).game
    assert game is not None
    assert any("bug_line" in p for p in quiz_item_problems(game))


def test_one_broken_item_is_tolerated_while_four_valid_final_items_remain() -> None:
    def broken_game_and_extra(data: dict[str, Any]) -> None:
        data["game"]["bug_line"] = 20
        data["sections"][0]["check"] = copy.deepcopy(data["final_quiz"][0])
        data["sections"][0]["check"]["correct_index"] = 4
        data["final_quiz"].append(copy.deepcopy(data["final_quiz"][3]))
        data["final_quiz"][3]["pairs"] = [["same", "a"], ["same", "b"], ["other", "c"]]

    assert _problems(broken_game_and_extra) == []


def test_mcq_game_is_a_problem() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["game"] = copy.deepcopy(data["final_quiz"][0])

    assert any("game type" in p for p in _problems(mutate))


def test_explorable_range_is_checked() -> None:
    def inverted(data: dict[str, Any]) -> None:
        data["explorable"]["min"] = 100

    def outside(data: dict[str, Any]) -> None:
        data["explorable"]["points"][0]["at"] = -5

    assert any("min must be below max" in p for p in _problems(inverted))
    assert any("outside range" in p for p in _problems(outside))


def test_quiz_variety_is_a_soft_rule_naming_missing_types() -> None:
    data = copy.deepcopy(lesson_data())
    data["final_quiz"] = [copy.deepcopy(data["final_quiz"][0]) for _ in range(4)]
    lesson = LessonSpec.model_validate(data)
    assert lesson_problems(lesson) == []
    variety = quiz_variety_problems(lesson)
    assert len(variety) == 1
    for missing in ("ordering", "short_answer", "spot_the_bug", "match_pairs"):
        assert missing in variety[0]
    assert quiz_variety_problems(LessonSpec.model_validate(lesson_data())) == []


def test_generated_rejects_negative_tokens() -> None:
    with pytest.raises(AssertionError):
        Generated(value=1, model="m", input_tokens=-1, output_tokens=0)


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_infinity_and_nan_are_rejected(bad: float) -> None:
    data = lesson_data()
    data["explorable"]["points"][0]["at"] = bad
    with pytest.raises(ValidationError, match="finite"):
        LessonSpec.model_validate(data)


def test_json_infinity_is_rejected() -> None:
    raw = json.dumps(lesson_data()).replace('"max": 30', '"max": Infinity', 1)
    assert "Infinity" in raw
    with pytest.raises(ValidationError):
        LessonSpec.model_validate_json(raw)


def _set_digest(path: str, value: Any) -> dict[str, Any]:
    data = digest_data()
    data[path][0] = value
    return data


@pytest.mark.parametrize(("field", "limit"), [("tldr", 200), ("gotchas", 300), ("topics", 40)])
def test_digest_list_items_are_bounded(field: str, limit: int) -> None:
    PostDigest.model_validate(_set_digest(field, "x" * limit))
    with pytest.raises(ValidationError, match="at most"):
        PostDigest.model_validate(_set_digest(field, "x" * (limit + 1)))


def _quiz_with(kind: str) -> tuple[dict[str, Any], dict[str, Any]]:
    data = lesson_data()
    items = [*data["final_quiz"], data["game"]]
    items += [s["check"] for s in data["sections"] if s.get("check")]
    return data, next(item for item in items if item["type"] == kind)


@pytest.mark.parametrize(
    ("kind", "field", "limit"),
    [
        ("mcq", "options", 200),
        ("ordering", "steps_in_order", 200),
        ("short_answer", "accepted_answers", 100),
        ("spot_the_bug", "lines", 200),
    ],
)
def test_quiz_list_items_are_bounded(kind: str, field: str, limit: int) -> None:
    data, item = _quiz_with(kind)
    item[field][0] = "x" * limit
    LessonSpec.model_validate(data)
    item[field][0] = "x" * (limit + 1)
    with pytest.raises(ValidationError, match="at most"):
        LessonSpec.model_validate(data)


def test_match_pairs_items_are_bounded() -> None:
    pairs = [["a", "1"], ["b", "2"], ["c", "x" * 100]]
    data = lesson_data()
    data["game"] = {
        "type": "match_pairs",
        "source_quote": "The check and the write must happen",
        "prompt": "Match",
        "pairs": pairs,
        "explanation": "e",
    }
    LessonSpec.model_validate(data)
    pairs[2][1] = "x" * 101
    with pytest.raises(ValidationError, match="at most"):
        LessonSpec.model_validate(data)


def test_final_quiz_may_be_empty_but_generation_requires_four() -> None:
    def empty(data: dict[str, Any]) -> None:
        data["final_quiz"] = []

    assert any(f"at least {FINAL_QUIZ_MIN}" in p for p in _problems(empty))
