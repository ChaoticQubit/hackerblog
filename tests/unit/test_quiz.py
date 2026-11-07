import sqlite3
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from tests.samples import lesson_data
from tests.unit.test_search import add_post, add_site, make_db

from hackerblog.core.llm.schemas import MCQ, LessonSpec, MatchPairs, Ordering, QuizItem, SpotTheBug
from hackerblog.core.quiz import (
    AnswerFormatError,
    grade,
    normalise,
    present,
    record_quiz,
    shuffled,
)

ITEMS = LessonSpec.model_validate(lesson_data()).final_quiz
MCQ_ITEM, ORDERING_ITEM, SHORT_ITEM, PAIRS_ITEM = ITEMS
BUG_ITEM = SpotTheBug.model_validate(lesson_data()["game"])


def test_shuffled_is_a_seeded_non_identity_permutation() -> None:
    for seed in range(50):
        order = shuffled(4, seed)
        assert sorted(order) == [0, 1, 2, 3]
        assert order != [0, 1, 2, 3]
        assert shuffled(4, seed) == order
    assert len({tuple(shuffled(5, seed)) for seed in range(50)}) > 5


def test_mcq_by_number() -> None:
    assert isinstance(MCQ_ITEM, MCQ)
    assert grade(MCQ_ITEM, "3", 1).correct
    assert not grade(MCQ_ITEM, "1", 1).correct
    assert "One atomic check and write" in grade(MCQ_ITEM, "1", 1).expected


@pytest.mark.parametrize("answer", ["", "x", "0", "4", "1 2"])
def test_mcq_rejects_malformed_input(answer: str) -> None:
    with pytest.raises(AnswerFormatError):
        grade(MCQ_ITEM, answer, 1)


def test_ordering_follows_the_displayed_shuffle() -> None:
    assert isinstance(ORDERING_ITEM, Ordering)
    seed = 7
    shown = present(ORDERING_ITEM, seed).choices
    order = [shown.index(step) + 1 for step in ORDERING_ITEM.steps_in_order]
    assert grade(ORDERING_ITEM, " ".join(map(str, order)), seed).correct
    assert not grade(ORDERING_ITEM, "1 2 3 4", seed).correct
    assert ORDERING_ITEM.steps_in_order[0] in grade(ORDERING_ITEM, "1 2 3 4", seed).expected


@pytest.mark.parametrize("answer", ["1 2 3", "1 1 2 3", "1 2 3 9", "a b c d", ""])
def test_ordering_rejects_malformed_input(answer: str) -> None:
    with pytest.raises(AnswerFormatError):
        grade(ORDERING_ITEM, answer, 7)


def test_ordering_accepts_commas() -> None:
    shown = present(ORDERING_ITEM, 3).choices
    assert isinstance(ORDERING_ITEM, Ordering)
    order = [shown.index(step) + 1 for step in ORDERING_ITEM.steps_in_order]
    assert grade(ORDERING_ITEM, ",".join(map(str, order)), 3).correct


def test_short_answer_is_normalised() -> None:
    assert grade(SHORT_ITEM, "  Network   JITTER! ", 1).correct
    assert grade(SHORT_ITEM, "the jitter", 1).correct
    assert not grade(SHORT_ITEM, "latency", 1).correct
    assert normalise("Ünïcode  CASE.") == "ünïcode case"


def test_short_answer_rejects_blank() -> None:
    with pytest.raises(AnswerFormatError):
        grade(SHORT_ITEM, "   ", 1)


def test_spot_the_bug_by_one_based_line() -> None:
    assert grade(BUG_ITEM, "5", 1).correct
    assert not grade(BUG_ITEM, "4", 1).correct
    with pytest.raises(AnswerFormatError):
        grade(BUG_ITEM, "6", 1)
    assert present(BUG_ITEM, 1).choices[4] == "write_uses(code, uses - 1)"


def test_match_pairs_by_number_and_letter() -> None:
    assert isinstance(PAIRS_ITEM, MatchPairs)
    seed = 11
    rights = present(PAIRS_ITEM, seed).targets
    letters = "abcdef"
    answer = " ".join(
        f"{n + 1}{letters[rights.index(right)]}" for n, (_, right) in enumerate(PAIRS_ITEM.pairs)
    )
    assert grade(PAIRS_ITEM, answer, seed).correct
    assert grade(PAIRS_ITEM, answer.upper().replace(" ", ","), seed).correct
    wrong = answer.replace("1", "#").replace("2", "1").replace("#", "2")
    assert not grade(PAIRS_ITEM, wrong, seed).correct


@pytest.mark.parametrize("answer", ["1a 2b", "1a 1b 2c", "1a 2a 3b", "4a 1b 2c", "1z 2a 3b", "x"])
def test_match_pairs_rejects_malformed_input(answer: str) -> None:
    with pytest.raises(AnswerFormatError):
        grade(PAIRS_ITEM, answer, 11)


def test_present_returns_question_text_for_every_type() -> None:
    adapter: TypeAdapter[QuizItem] = TypeAdapter(QuizItem)
    for item in [*ITEMS, BUG_ITEM]:
        prompt = present(adapter.validate_python(item.model_dump()), 5)
        assert prompt.text
        assert prompt.how


def record_db(tmp_path: Path) -> tuple[sqlite3.Connection, int]:
    conn = make_db(tmp_path)
    post_id = add_post(conn, add_site(conn, "alpha"), "Limit overrun races")
    return conn, post_id


def reading(conn: sqlite3.Connection, post_id: int) -> str:
    return conn.execute("SELECT reading_status FROM posts WHERE id = ?", (post_id,)).fetchone()[0]


def test_record_quiz_below_threshold_keeps_status(tmp_path: Path) -> None:
    conn, post_id = record_db(tmp_path)
    assert record_quiz(conn, post_id, 3, 4) is False
    assert reading(conn, post_id) == "unread"
    row = conn.execute("SELECT post_id, score, total FROM quiz_attempts").fetchone()
    assert row == (post_id, 3, 4)


def test_record_quiz_at_80_percent_sets_learned(tmp_path: Path) -> None:
    conn, post_id = record_db(tmp_path)
    assert record_quiz(conn, post_id, 4, 5) is True
    assert reading(conn, post_id) == "learned"


def test_record_quiz_leaves_archived_posts_archived(tmp_path: Path) -> None:
    conn, post_id = record_db(tmp_path)
    conn.execute("UPDATE posts SET reading_status = 'archived' WHERE id = ?", (post_id,))
    assert record_quiz(conn, post_id, 5, 5) is False
    assert reading(conn, post_id) == "archived"
    assert conn.execute("SELECT count(*) FROM quiz_attempts").fetchone()[0] == 1
