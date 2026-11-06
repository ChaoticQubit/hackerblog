"""Valid digest and lesson payloads grounded in tests/fixtures/posts/race-conditions.html.

Every source_quote below occurs in that fixture, so these pass the grounding check. Each
call returns a fresh deep copy that tests may mutate.
"""

from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"
RACE_POST_HTML = FIXTURES / "posts" / "race-conditions.html"
RACE_POST_URL = "https://blog.example.com/posts/limit-overrun-races"


def _idea(claim: str, detail: str, icon: str) -> dict[str, str]:
    return {"claim": claim, "detail": detail, "icon": icon}


def digest_data() -> dict[str, Any]:
    """Return a PostDigest-shaped dict for the race-conditions fixture post."""
    return {
        "title": "Limit overrun races in web apps",
        "tldr": [
            "A check followed by a separate write lets parallel requests exceed a limit.",
            "The single-packet attack makes 20 to 30 requests finish within a millisecond.",
            "Let the database enforce the limit with one conditional update.",
        ],
        "why_it_matters": "Coupons, gift cards and rate limits all break the same way.",
        "summary": "Limit overrun races happen between a check and an update. HTTP/2 lets an "
        "attacker align request completion so jitter no longer hides the window.",
        "key_ideas": [
            _idea("Check then write is a race", "Two steps leave a gap.", "bug"),
            _idea("Align the finish", "Withhold the last byte.", "network"),
            _idea("One packet beats jitter", "Final bytes travel together.", "clock"),
            _idea("Atomic fix", "Use a conditional UPDATE.", "database"),
        ],
        "notes_markdown": "## Race window\n\nThe window is **milliseconds** long.\n\n"
        "- Check\n- Write\n\n```sql\nUPDATE coupons SET uses = uses - 1 WHERE uses > 0\n```\n",
        "glossary": [
            {"term": "Limit overrun", "definition": "Exceeding a limit through a race."},
            {"term": "Single-packet attack", "definition": "Final bytes sent in one packet."},
        ],
        "gotchas": ["A delay only moves the window."],
        "snippets": [
            {
                "language": "python",
                "code": "uses = read()\nif uses <= 0:\n    raise ValueError\nwrite(uses - 1)",
                "explanation": "The read and the write are separate steps.",
            }
        ],
        "topics": ["race conditions", "http2"],
        "difficulty": "intermediate",
    }


def _mcq() -> dict[str, Any]:
    return {
        "type": "mcq",
        "source_quote": "The check and the write must happen in one atomic step.",
        "question": "What removes a limit overrun race?",
        "options": ["A delay", "A cache", "One atomic check and write"],
        "correct_index": 2,
        "explanation": "Delays and caches only move or copy the window.",
    }


def _ordering() -> dict[str, Any]:
    return {
        "type": "ordering",
        "source_quote": "The single-packet attack works in four steps.",
        "prompt": "Order the steps of the single-packet attack.",
        "steps_in_order": [
            "Open one connection",
            "Send headers and most of each body",
            "Withhold the final byte of each request",
            "Send every final byte in one packet",
        ],
        "explanation": "Completion is aligned, not start.",
    }


def _short_answer() -> dict[str, Any]:
    return {
        "type": "short_answer",
        "source_quote": "Network jitter is usually larger than that",
        "question": "What hides the race window when requests are sent one by one?",
        "accepted_answers": ["network jitter", "jitter"],
        "explanation": "Jitter is larger than the window.",
    }


def _spot_the_bug() -> dict[str, Any]:
    return {
        "type": "spot_the_bug",
        "source_quote": "then writes the new value in a separate statement",
        "prompt": "Which line lets two requests both succeed?",
        "language": "python",
        "lines": [
            "uses = read_uses(code)",
            "if uses <= 0:",
            "    raise ValueError('used up')",
            "apply_discount(code)",
            "write_uses(code, uses - 1)",
        ],
        "bug_line": 4,
        "explanation": "The write uses a stale read instead of a conditional update.",
    }


def _match_pairs() -> dict[str, Any]:
    return {
        "type": "match_pairs",
        "source_quote": "Locks and serializable transactions also work",
        "prompt": "Match each idea with its effect.",
        "pairs": [
            ["Delay", "Moves the window"],
            ["Cache", "Adds a second copy that can race"],
            ["Conditional update", "Changes one row or none"],
        ],
        "explanation": "Only the database can enforce the limit atomically.",
    }


def lesson_data() -> dict[str, Any]:
    """Return a LessonSpec-shaped dict for the race-conditions fixture post."""
    return {
        "diagram": {
            "kind": "flow",
            "title": "Single-packet attack",
            "nodes": [
                {"label": "Open connection", "detail": "One HTTP/2 connection."},
                {"label": "Withhold last byte", "detail": "No request completes yet."},
                {"label": "Release together", "detail": "All final bytes in one packet."},
            ],
        },
        "sections": [
            {
                "heading": "The race window",
                "body_markdown": "A **check** and a separate **write** leave a gap.",
                "check": _short_answer(),
            },
            {
                "heading": "Fixing it",
                "body_markdown": "Use `UPDATE ... WHERE uses > 0` and check the row count.",
                "check": None,
            },
        ],
        "explorable": {
            "label": "Parallel requests",
            "unit": "requests",
            "min": 1,
            "max": 30,
            "step": 1,
            "points": [
                {"at": 1, "outcome": "One request never races."},
                {"at": 20, "outcome": "Twenty requests land inside the window."},
            ],
        },
        "game": _spot_the_bug(),
        "final_quiz": [_mcq(), _ordering(), _short_answer(), _match_pairs()],
    }
