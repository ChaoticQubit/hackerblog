"""Grounding check: quiz items must quote the post they test (section 11.3, threat T3).

Every quiz item carries a source_quote. After normalising whitespace, quotes, case and
Markdown emphasis, the quote must occur in the post body, or the item is dropped. When
fewer than FINAL_QUIZ_MIN final items survive, the final quiz is emptied and the lesson is
flagged quiz_ungrounded; the pipeline retries the lesson stage once on that flag, then
renders the lesson without a final quiz.
"""

import re
from typing import NamedTuple

from hackerblog.core.llm.schemas import (
    FINAL_QUIZ_MIN,
    MCQ,
    Grounded,
    LessonSpec,
    MatchPairs,
    Ordering,
    ShortAnswer,
    SpotTheBug,
    quiz_item_problems,
)

QUIZ_UNGROUNDED = "quiz_ungrounded"
QUOTE_CHARS_MIN = 12
QUOTE_MARKS = re.compile("[\"'\u2018\u2019\u201a\u201b\u201c\u201d\u201e\u201f\u00ab\u00bb`*_]")
WHITESPACE = re.compile(r"\s+")


class GroundedLesson(NamedTuple):
    """The lesson with ungrounded items removed, how many were removed, and post flags.

    When QUIZ_UNGROUNDED is in flags, lesson.final_quiz is empty and the lesson renders
    without a final quiz. The lesson is re-validated, so it round-trips through JSON.
    """

    lesson: LessonSpec
    dropped: int
    flags: tuple[str, ...]

    @property
    def quiz_grounded(self) -> bool:
        """True when at least FINAL_QUIZ_MIN final quiz items are grounded."""
        return QUIZ_UNGROUNDED not in self.flags


def normalise(text: str) -> str:
    """Casefold text, drop quote marks, backticks and emphasis marks, and collapse whitespace."""
    folded = WHITESPACE.sub(" ", QUOTE_MARKS.sub("", text)).strip().casefold()
    assert "  " not in folded
    assert folded == folded.strip()
    return folded


def is_grounded(quote: str, body_normalised: str) -> bool:
    """True when the normalised quote is long enough and occurs in the normalised body."""
    needle = normalise(quote)
    return len(needle) >= QUOTE_CHARS_MIN and needle in body_normalised


def ground_lesson(lesson: LessonSpec, body_md: str) -> GroundedLesson:
    """Drop every quiz item whose source_quote is not in body_md or that breaks an item rule.

    Covers final_quiz, section checks and the game. Returns the cleaned lesson, the number
    of dropped items and the flags to store on the post.
    """
    body = normalise(body_md)

    def keep(item: Grounded) -> bool:
        assert isinstance(item, MCQ | Ordering | ShortAnswer | SpotTheBug | MatchPairs)
        return is_grounded(item.source_quote, body) and not quiz_item_problems(item)

    final_quiz = [item for item in lesson.final_quiz if keep(item)]
    sections = [
        section
        if section.check is None or keep(section.check)
        else section.model_copy(update={"check": None})
        for section in lesson.sections
    ]
    game = lesson.game
    if game is not None and not keep(game):
        game = None
    dropped = (
        len(lesson.final_quiz)
        - len(final_quiz)
        + sum(a.check is not b.check for a, b in zip(lesson.sections, sections, strict=True))
        + (game is not lesson.game)
    )
    flags: tuple[str, ...] = ()
    if len(final_quiz) < FINAL_QUIZ_MIN:
        final_quiz, flags = [], (QUIZ_UNGROUNDED,)
    grounded = LessonSpec.model_validate(
        lesson.model_copy(
            update={"final_quiz": final_quiz, "sections": sections, "game": game}
        ).model_dump()
    )
    assert 0 <= dropped <= len(lesson.final_quiz) + len(lesson.sections) + 1
    assert not grounded.final_quiz or len(grounded.final_quiz) >= FINAL_QUIZ_MIN
    return GroundedLesson(grounded, dropped, flags)
