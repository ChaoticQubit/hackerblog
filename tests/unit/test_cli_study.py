import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.samples import lesson_data
from tests.unit.test_search import add_post, add_site
from typer.testing import CliRunner

from hackerblog.cli.commands.study import answer_sources, play_quiz
from hackerblog.cli.main import app
from hackerblog.core.llm.schemas import Answer, LessonSpec, MatchPairs, Ordering
from hackerblog.core.quiz import present
from hackerblog.core.store.migrate import connect, migrate

runner = CliRunner()
EVIL_TITLE = "Evil \x1b]0;pwned\x07\x1b[2J title\u202e"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = tmp_path / "hb"
    home.mkdir(mode=0o700)
    conn = connect(home / "hackerblog.db")
    migrate(conn)
    alpha = add_site(conn, "alpha")
    beta = add_site(conn, "beta")
    out = tmp_path / "out"
    add_post(conn, alpha, "Limit overrun races", body="race window", out_dir=out)
    add_post(conn, beta, EVIL_TITLE, body="races everywhere", out_dir=out)
    add_post(conn, beta, "Queued post", status="queued")
    add_post(conn, beta, "No files", body="ready but unrendered")
    conn.close()
    return home


@pytest.fixture
def env(home: Path, tmp_path: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path),
        "HACKERBLOG_HOME": str(home),
        "HACKERBLOG_CONFIG": "",
        "ANTHROPIC_API_KEY": "",
        "NO_COLOR": "1",
    }


def invoke(env: dict[str, str], *args: str, stdin: str | None = None) -> tuple[int, str]:
    result = runner.invoke(app, list(args), env=env, input=stdin)
    return result.exit_code, result.output


def db(home: Path) -> sqlite3.Connection:
    return connect(home / "hackerblog.db")


def test_search_prints_hits_with_marked_snippet(env: dict[str, str]) -> None:
    code, out = invoke(env, "search", "race")
    assert code == 0, out
    assert "Limit overrun races" in out
    assert "alpha" in out
    assert "[race]" in out or "[races]" in out


def test_search_strips_terminal_escapes(env: dict[str, str]) -> None:
    code, out = invoke(env, "search", "races")
    assert code == 0, out
    assert "\x1b" not in out
    assert "\x07" not in out
    assert "\u202e" not in out
    assert "Evil" in out


def test_search_json_and_site_filter(env: dict[str, str]) -> None:
    code, out = invoke(env, "search", "race", "--site", "alpha", "--json")
    assert code == 0, out
    payload = json.loads(out)
    assert payload["ok"] is True
    assert [hit["site"] for hit in payload["data"]] == ["alpha"]
    assert set(payload["data"][0]) == {"post_id", "site", "title", "snippet"}


@pytest.mark.parametrize("query", ['"', "NEAR(a b)", "title:race", "*", "a OR"])
def test_search_injection_inputs_exit_cleanly(env: dict[str, str], query: str) -> None:
    code, out = invoke(env, "search", query)
    assert code == 0, out


def test_search_limit_bounds_and_unknown_site(env: dict[str, str]) -> None:
    assert invoke(env, "search", "race", "--limit", "0")[0] == 2
    assert invoke(env, "search", "race", "--limit", "101")[0] == 2
    assert invoke(env, "search", "  ")[0] == 2
    code, out = invoke(env, "search", "race", "--site", "nope")
    assert code == 4
    assert "hackerblog sites list" in out


def test_search_without_database_exits_3(env: dict[str, str], tmp_path: Path) -> None:
    code, out = invoke({**env, "HACKERBLOG_HOME": str(tmp_path / "none")}, "search", "x")
    assert code == 3
    assert "hackerblog init" in out


def test_open_print_marks_read(env: dict[str, str], home: Path) -> None:
    code, out = invoke(env, "open", "1", "--print")
    assert code == 0, out
    assert out.strip().endswith("lesson.html")
    status = db(home).execute("SELECT reading_status FROM posts WHERE id = 1").fetchone()[0]
    assert status == "read"


def test_open_print_notes_and_source(env: dict[str, str]) -> None:
    assert invoke(env, "open", "1", "--notes", "--print")[1].strip().endswith("notes.md")
    assert invoke(env, "open", "1", "--source", "--print")[1].startswith("https://example.com/")
    assert invoke(env, "open", "1", "--notes", "--source", "--print")[0] == 2


def test_open_keeps_learned_status(env: dict[str, str], home: Path) -> None:
    conn = db(home)
    conn.execute("UPDATE posts SET reading_status = 'learned' WHERE id = 1")
    conn.close()
    assert invoke(env, "open", "1", "--print")[0] == 0
    status = db(home).execute("SELECT reading_status FROM posts WHERE id = 1").fetchone()[0]
    assert status == "learned"


def test_open_not_ready_or_unknown_exits_4(env: dict[str, str]) -> None:
    assert invoke(env, "open", "3", "--print")[0] == 4
    assert invoke(env, "open", "999", "--print")[0] == 4
    code, out = invoke(env, "open", "4", "--print")
    assert code == 4
    assert "regenerate" in out


def test_quiz_requires_tty(env: dict[str, str], home: Path) -> None:
    code, out = invoke(env, "quiz", "1", stdin="1\n")
    assert code == 2
    assert "terminal" in out
    assert db(home).execute("SELECT count(*) FROM quiz_attempts").fetchone()[0] == 0


def test_quiz_unknown_or_not_ready_exits_4(env: dict[str, str]) -> None:
    assert invoke(env, "quiz", "999")[0] == 4
    assert invoke(env, "quiz", "3")[0] == 4


def correct_answers(seed: int) -> list[str]:
    lesson = LessonSpec.model_validate(lesson_data())
    mcq, ordering, _, pairs = lesson.final_quiz
    assert mcq.type == "mcq"
    assert isinstance(ordering, Ordering)
    assert isinstance(pairs, MatchPairs)
    shown = present(ordering, seed).choices
    order = " ".join(str(shown.index(step) + 1) for step in ordering.steps_in_order)
    rights = present(pairs, seed).targets
    match = " ".join(f"{n + 1}{'abcdef'[rights.index(r)]}" for n, (_, r) in enumerate(pairs.pairs))
    return [str(mcq.correct_index + 1), order, "jitter", match]


def scripted(answers: list[str]) -> Iterator[str]:
    yield from answers


def test_play_quiz_records_and_sets_learned(home: Path) -> None:
    conn = db(home)
    lines: list[str] = []
    answers = scripted(["nonsense", *correct_answers(9)])
    result = play_quiz(conn, 1, 9, lambda _: next(answers), lines.append)
    assert (result.score, result.total, result.learned) == (4, 4, True)
    assert any("Score: 4/4" in line for line in lines)
    assert conn.execute("SELECT score, total FROM quiz_attempts").fetchall() == [(4, 4)]
    status = conn.execute("SELECT reading_status FROM posts WHERE id = 1").fetchone()[0]
    assert status == "learned"


def test_play_quiz_low_score_stays_unread(home: Path) -> None:
    conn = db(home)
    answers = scripted(["1", "1 2 3 4", "latency", "1a 2b 3c"])
    result = play_quiz(conn, 1, 9, lambda _: next(answers), lambda _: None)
    assert result.score <= 1
    status = conn.execute("SELECT reading_status FROM posts WHERE id = 1").fetchone()[0]
    assert status == "unread"


def test_play_quiz_interrupt_records_nothing(home: Path) -> None:
    conn = db(home)
    answers = scripted(["3"])

    def read(_: str) -> str:
        for answer in answers:
            return answer
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        play_quiz(conn, 1, 9, read, lambda _: None)
    assert conn.execute("SELECT count(*) FROM quiz_attempts").fetchone()[0] == 0


def test_play_quiz_cleans_untrusted_text(home: Path) -> None:
    conn = db(home)
    answers = scripted(correct_answers(2))
    lines: list[str] = []
    play_quiz(conn, 2, 2, lambda _: next(answers), lines.append)
    assert all("\x1b" not in line for line in lines)


def test_ask_validation(env: dict[str, str]) -> None:
    assert invoke(env, "ask")[0] == 2
    assert invoke(env, "ask", "-i", "--json")[0] == 2
    assert invoke(env, "ask", "why?", "--post", "999")[0] == 4
    assert invoke(env, "ask", "why?", "--site", "nope")[0] == 4


def test_ask_runs_against_backend(env: dict[str, str]) -> None:
    pytest.importorskip(
        "hackerblog.core.llm.agno_backend",
        reason="agno_backend.answer comes from @proc-llm and is not on this branch yet",
    )
    code, out = invoke(env, "ask", "What is a race window?", "--json")
    assert code in {0, 3, 7, 10}, out


def test_ask_sources_keep_only_ready_posts_of_the_site(home: Path) -> None:
    conn = db(home)
    answer = Answer(text="x", post_ids=[3, 1, 2, 999, 2, 4])
    assert [source["id"] for source in answer_sources(conn, answer, None)] == [1, 2, 4]
    assert [source["id"] for source in answer_sources(conn, answer, "beta")] == [2, 4]
    assert answer_sources(conn, answer, "alpha")[0]["lesson"]
