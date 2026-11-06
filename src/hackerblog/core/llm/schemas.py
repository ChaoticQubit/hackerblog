"""LLM output schemas and the shared contracts of the processing stream (section 11.3).

Every model forbids extra keys and bounds every string and list, so a hostile or rambling
model output cannot produce an unbounded page. Post-validation that JSON Schema cannot
express lives in validate_digest and validate_lesson.
"""

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1"
MCQ_OPTIONS_MAX = 5
BUG_LINES_MAX = 30
FINAL_QUIZ_MIN = 4
FINAL_QUIZ_TYPES_MIN = 3
GAME_TYPES = frozenset({"ordering", "spot_the_bug", "match_pairs"})

Icon = Literal[
    "shield", "bug", "key", "lock", "network", "server", "database", "code",
    "terminal", "cloud", "user", "alert", "check", "idea", "book", "gear",
    "chart", "clock", "link", "search", "flag", "layers", "package", "eye",
]  # fmt: skip
Language = Annotated[str, Field(max_length=20, pattern=r"^[a-z0-9+#-]*$")]
Stage = Literal["digest", "lesson"]


class Strict(BaseModel):
    """Base for every LLM output model: unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid")


class KeyIdea(Strict):
    """One headline idea of a post, shown as an icon card."""

    claim: str = Field(max_length=160)
    detail: str = Field(max_length=800)
    icon: Icon


class Term(Strict):
    """A glossary entry, also used to build flashcards."""

    term: str = Field(max_length=60)
    definition: str = Field(max_length=300)


class Snippet(Strict):
    """An annotated code snippet taken from the post."""

    language: Language
    code: str = Field(max_length=4000)
    explanation: str = Field(max_length=600)


class PostDigest(Strict):
    """Output of the digest stage: the summary and notes of one post."""

    title: str = Field(max_length=200)
    tldr: list[str] = Field(min_length=3, max_length=3)
    why_it_matters: str = Field(max_length=400)
    summary: str = Field(max_length=900)
    key_ideas: list[KeyIdea] = Field(min_length=4, max_length=7)
    notes_markdown: str = Field(max_length=12000)
    glossary: list[Term] = Field(max_length=15)
    gotchas: list[str] = Field(max_length=8)
    snippets: list[Snippet] = Field(max_length=6)
    topics: list[str] = Field(max_length=8)
    difficulty: Literal["intro", "intermediate", "advanced"]


class Grounded(Strict):
    """Base for quiz items: source_quote must occur in the post body (grounding check)."""

    source_quote: str = Field(min_length=12, max_length=240)


class MCQ(Grounded):
    """Multiple choice question."""

    type: Literal["mcq"]
    question: str = Field(max_length=300)
    options: list[str] = Field(min_length=2, max_length=MCQ_OPTIONS_MAX)
    correct_index: int = Field(ge=0, le=MCQ_OPTIONS_MAX - 1)
    explanation: str = Field(max_length=500)


class Ordering(Grounded):
    """Put the steps in the right order."""

    type: Literal["ordering"]
    prompt: str = Field(max_length=300)
    steps_in_order: list[str] = Field(min_length=3, max_length=7)
    explanation: str = Field(max_length=500)


class ShortAnswer(Grounded):
    """Free text answer matched against accepted answers."""

    type: Literal["short_answer"]
    question: str = Field(max_length=300)
    accepted_answers: list[str] = Field(min_length=1, max_length=5)
    explanation: str = Field(max_length=500)


class SpotTheBug(Grounded):
    """Pick the buggy line of a code listing."""

    type: Literal["spot_the_bug"]
    prompt: str = Field(max_length=300)
    language: Language
    lines: list[str] = Field(min_length=2, max_length=BUG_LINES_MAX)
    bug_line: int = Field(ge=0, le=BUG_LINES_MAX - 1)
    explanation: str = Field(max_length=500)


class MatchPairs(Grounded):
    """Match each left item with its right item."""

    type: Literal["match_pairs"]
    prompt: str = Field(max_length=300)
    pairs: list[tuple[str, str]] = Field(min_length=3, max_length=6)
    explanation: str = Field(max_length=500)


QuizItem = Annotated[
    MCQ | Ordering | ShortAnswer | SpotTheBug | MatchPairs, Field(discriminator="type")
]


class DiagramNode(Strict):
    """One box, band or dot of a diagram."""

    label: str = Field(max_length=40)
    detail: str = Field(max_length=200)


class Diagram(Strict):
    """A diagram the template draws as inline SVG."""

    kind: Literal["flow", "layers", "timeline"]
    title: str = Field(max_length=80)
    nodes: list[DiagramNode] = Field(min_length=2, max_length=8)


class ExplorablePoint(Strict):
    """Outcome text shown when the slider is nearest to `at`."""

    at: float
    outcome: str = Field(max_length=200)


class Explorable(Strict):
    """A range slider with outcomes at chosen points."""

    label: str = Field(max_length=80)
    unit: str = Field(max_length=16)
    min: float
    max: float
    step: float = Field(gt=0)
    points: list[ExplorablePoint] = Field(min_length=2, max_length=10)


class Section(Strict):
    """A lesson section with an optional embedded check."""

    heading: str = Field(max_length=100)
    body_markdown: str = Field(max_length=3000)
    check: QuizItem | None = None


class LessonSpec(Strict):
    """Output of the lesson stage: everything the lesson template needs beyond the digest."""

    diagram: Diagram | None
    sections: list[Section] = Field(min_length=2, max_length=6)
    explorable: Explorable | None
    game: QuizItem | None
    final_quiz: list[QuizItem] = Field(min_length=FINAL_QUIZ_MIN, max_length=8)


class Answer(Strict):
    """Output of `ask`: plain text plus the ids of the posts it used."""

    text: str = Field(max_length=8000)
    post_ids: list[int] = Field(default_factory=list[int], max_length=20)


@dataclass(frozen=True)
class PostContext:
    """What a generation stage reads about one post. body_md is already cut to the limit."""

    post_id: int
    title: str
    url: str
    site_name: str
    site_tags: tuple[str, ...]
    author: str | None
    published_at: str | None
    body_md: str
    truncated: bool
    content_hash: str


@dataclass(frozen=True)
class Generated[T]:
    """A validated model output plus the model id and token counts of the call."""

    value: T
    model: str
    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        assert self.input_tokens >= 0
        assert self.output_tokens >= 0
        assert self.model


class InvalidModelOutputError(ValueError):
    """The model output passed the schema but failed post-validation, or never parsed."""


def quiz_item_problems(item: MCQ | Ordering | ShortAnswer | SpotTheBug | MatchPairs) -> list[str]:
    """Return why one quiz item is internally inconsistent; empty when it is valid."""
    problems: list[str] = []
    if isinstance(item, MCQ) and item.correct_index >= len(item.options):
        problems.append(f"mcq correct_index {item.correct_index} >= {len(item.options)} options")
    if isinstance(item, SpotTheBug) and item.bug_line >= len(item.lines):
        problems.append(f"spot_the_bug bug_line {item.bug_line} >= {len(item.lines)} lines")
    if isinstance(item, MatchPairs) and len({left for left, _ in item.pairs}) != len(item.pairs):
        problems.append("match_pairs has duplicate left items")
    return problems


def lesson_problems(lesson: LessonSpec) -> list[str]:
    """Return every post-validation problem of a schema-valid lesson (section 11.3)."""
    problems: list[str] = []
    checks = [section.check for section in lesson.sections if section.check is not None]
    games = [lesson.game] if lesson.game is not None else []
    for item in [*lesson.final_quiz, *checks, *games]:
        problems.extend(quiz_item_problems(item))
    if lesson.game is not None and lesson.game.type not in GAME_TYPES:
        problems.append(f"game type {lesson.game.type} is not one of {sorted(GAME_TYPES)}")
    explorable = lesson.explorable
    if explorable is not None:
        if explorable.min >= explorable.max:
            problems.append("explorable min must be below max")
        outside = [p.at for p in explorable.points if not explorable.min <= p.at <= explorable.max]
        if outside:
            problems.append(f"explorable points outside range: {outside}")
    types = {item.type for item in lesson.final_quiz}
    if len(types) < FINAL_QUIZ_TYPES_MIN:
        problems.append(f"final_quiz needs {FINAL_QUIZ_TYPES_MIN} distinct types, has {len(types)}")
    return problems
