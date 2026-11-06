import copy
from typing import Any

import pytest
from pydantic import ValidationError
from tests.samples import digest_data, lesson_data

from hackerblog.core.llm.schemas import (
    Generated,
    LessonSpec,
    PostDigest,
    lesson_problems,
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


def test_bug_line_beyond_lines_is_a_problem() -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["game"]["bug_line"] = 20

    assert any("bug_line" in p for p in _problems(mutate))


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


def test_final_quiz_needs_three_types() -> None:
    def all_mcq(data: dict[str, Any]) -> None:
        data["final_quiz"] = [copy.deepcopy(data["final_quiz"][0]) for _ in range(4)]

    assert any("distinct types" in p for p in _problems(all_mcq))


def test_generated_rejects_negative_tokens() -> None:
    with pytest.raises(AssertionError):
        Generated(value=1, model="m", input_tokens=-1, output_tokens=0)
