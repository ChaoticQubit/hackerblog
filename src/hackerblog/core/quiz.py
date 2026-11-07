"""Terminal quiz: what to show for each quiz item, how to grade typed answers, and recording.

Grading is pure. Answer formats follow section 15.3: MCQ and spot the bug by 1-based number,
ordering as numbers over the shown steps such as `3 1 2 4`, short answer as normalised text,
match pairs as number-letter pairs such as `1b 2a 3c`. Shuffles are seeded so the grader sees
the same order the player saw.
"""

import hashlib
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime

from hackerblog.core.llm.schemas import MCQ, MatchPairs, Ordering, ShortAnswer, SpotTheBug

LEARNED_PERCENT_MIN = 80
LETTERS = "abcdefgh"
ARTICLES = ("the ", "a ", "an ")
SEPARATORS = re.compile(r"[\s,]+")
PAIR = re.compile(r"^(\d+)([a-z])$")
NON_WORD = re.compile(r"[^\w]+")

Item = MCQ | Ordering | ShortAnswer | SpotTheBug | MatchPairs


class AnswerFormatError(ValueError):
    """The typed answer does not fit the item's input format; the player should retype it."""


@dataclass(frozen=True)
class Prompt:
    """What to show: the question, numbered choices, lettered targets and how to answer."""

    text: str
    choices: tuple[str, ...]
    targets: tuple[str, ...]
    how: str


@dataclass(frozen=True)
class Graded:
    """Whether an answer was right, plus the expected answer in display form."""

    correct: bool
    expected: str


def shuffled(count: int, seed: int) -> list[int]:
    """Return a deterministic permutation of range(count) for seed, never the identity."""
    assert 2 <= count <= len(LETTERS), count
    digest = [hashlib.sha256(f"{seed}:{index}".encode()).digest() for index in range(count)]
    order = sorted(range(count), key=lambda index: digest[index])
    if order == list(range(count)):
        order = order[1:] + order[:1]
    assert sorted(order) == list(range(count))
    assert order != list(range(count))
    return order


def normalise(text: str) -> str:
    """Casefold, NFKC-normalise, turn punctuation runs into spaces and collapse whitespace."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    words = NON_WORD.sub(" ", folded).replace("_", " ").split()
    result = " ".join(words)
    assert result == result.strip()
    assert "  " not in result
    return result


def _strip_article(text: str) -> str:
    for article in ARTICLES:
        if text.startswith(article):
            return text[len(article) :]
    return text


def present(item: Item, seed: int) -> Prompt:
    """Build the prompt for one item; ordering steps and match targets use seed's shuffle."""
    match item:
        case MCQ():
            how = f"Type the number of your answer (1-{len(item.options)})."
            return Prompt(item.question, tuple(item.options), (), how)
        case Ordering():
            steps = item.steps_in_order
            shown = tuple(steps[index] for index in shuffled(len(steps), seed))
            return Prompt(item.prompt, shown, (), "Type the step numbers in order, e.g. 3 1 2.")
        case ShortAnswer():
            return Prompt(item.question, (), (), "Type a short answer.")
        case SpotTheBug():
            how = f"Type the number of the buggy line (1-{len(item.lines)})."
            return Prompt(item.prompt, tuple(item.lines), (), how)
        case MatchPairs():
            lefts = tuple(left for left, _ in item.pairs)
            rights = tuple(item.pairs[index][1] for index in shuffled(len(item.pairs), seed))
            return Prompt(item.prompt, lefts, rights, "Type pairs like 1b 2a 3c.")


def _number(answer: str, count: int) -> int:
    text = answer.strip()
    if not text.isdigit() or not 1 <= int(text) <= count:
        raise AnswerFormatError(f"Type one number from 1 to {count}.")
    return int(text) - 1


def _order(answer: str, count: int) -> list[int]:
    parts = [part for part in SEPARATORS.split(answer.strip()) if part]
    if len(parts) != count or not all(part.isdigit() for part in parts):
        raise AnswerFormatError(f"Type all {count} step numbers, e.g. 3 1 2.")
    numbers = [int(part) - 1 for part in parts]
    if sorted(numbers) != list(range(count)):
        raise AnswerFormatError(f"Use each number from 1 to {count} once.")
    return numbers


def _pairs(answer: str, count: int) -> dict[int, int]:
    parts = [part for part in SEPARATORS.split(answer.strip().lower()) if part]
    found = [PAIR.match(part) for part in parts]
    if len(parts) != count or not all(found):
        raise AnswerFormatError(f"Type {count} pairs like 1b 2a 3c.")
    mapping = {int(m.group(1)) - 1: LETTERS.find(m.group(2)) for m in found if m}
    if sorted(mapping) != list(range(count)) or sorted(mapping.values()) != list(range(count)):
        raise AnswerFormatError(f"Use each number 1-{count} and each letter once.")
    return mapping


def grade(item: Item, answer: str, seed: int) -> Graded:
    """Grade one typed answer; seed must be the one passed to present for this item.

    Raises AnswerFormatError when the answer does not fit the item's input format.
    """
    match item:
        case MCQ():
            chosen = _number(answer, len(item.options))
            expected = f"{item.correct_index + 1}. {item.options[item.correct_index]}"
            return Graded(chosen == item.correct_index, expected)
        case Ordering():
            shown = shuffled(len(item.steps_in_order), seed)
            picked = [shown[number] for number in _order(answer, len(shown))]
            expected = " ".join(str(shown.index(step) + 1) for step in range(len(shown)))
            steps = ", ".join(item.steps_in_order)
            return Graded(picked == list(range(len(shown))), f"{expected} ({steps})")
        case ShortAnswer():
            typed = _strip_article(normalise(answer))
            if not typed:
                raise AnswerFormatError("Type a short answer.")
            accepted = {_strip_article(normalise(text)) for text in item.accepted_answers}
            return Graded(typed in accepted, item.accepted_answers[0])
        case SpotTheBug():
            chosen = _number(answer, len(item.lines))
            expected = f"line {item.bug_line + 1}: {item.lines[item.bug_line]}"
            return Graded(chosen == item.bug_line, expected)
        case MatchPairs():
            shown = shuffled(len(item.pairs), seed)
            mapping = _pairs(answer, len(shown))
            right = all(shown[target] == left for left, target in mapping.items())
            letters = [f"{n + 1}{LETTERS[shown.index(n)]}" for n in range(len(shown))]
            return Graded(right, " ".join(letters))


def record_quiz(conn: sqlite3.Connection, post_id: int, score: int, total: int) -> bool:
    """Store one quiz attempt and set reading status learned at 80 percent or more.

    Archived posts stay archived. Runs in one transaction. Returns True when the post became
    or stayed learned through this attempt.
    """
    assert total > 0, total
    assert 0 <= score <= total, (score, total)
    passed = score * 100 >= total * LEARNED_PERCENT_MIN
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO quiz_attempts (post_id, score, total, created_at) VALUES (?, ?, ?, ?)",
            (post_id, score, total, now),
        )
        changed = 0
        if passed:
            changed = conn.execute(
                "UPDATE posts SET reading_status = 'learned', updated_at = ? "
                "WHERE id = ? AND reading_status IN ('unread', 'read', 'learned')",
                (now, post_id),
            ).rowcount
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    assert changed in {0, 1}
    return changed == 1
