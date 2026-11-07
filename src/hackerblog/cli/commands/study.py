"""Study commands: search, quiz, open and ask (section 15.3)."""

import asyncio
import importlib
import json
import os
import secrets
import sqlite3
import sys
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from hackerblog.cli.output import clean, emit_error, emit_json, say
from hackerblog.core.config import load_config
from hackerblog.core.credentials import PROVIDER_KEYS, lookup, read_secrets
from hackerblog.core.errors import ExitCode, HackerBlogError
from hackerblog.core.llm.ask_tools import build_tools
from hackerblog.core.llm.schemas import Answer, LessonSpec
from hackerblog.core.paths import Paths
from hackerblog.core.quiz import AnswerFormatError, Item, grade, present, record_quiz
from hackerblog.core.store.migrate import connect, migrate
from hackerblog.core.store.search import SEARCH_LIMIT_MAX, search

JsonFlag = Annotated[bool, typer.Option("--json", help="Print one JSON document.")]
ANSWER_TRIES_MAX = 3
ASK_TURNS_MAX = 50
HISTORY_TURNS_MAX = 20
LETTERS = "abcdefgh"
SOURCE_SQL = """
SELECT posts.title FROM posts JOIN sites ON sites.id = posts.site_id
WHERE posts.id = ? AND posts.status = 'ready' AND (? IS NULL OR sites.slug = ?)
"""


@dataclass(frozen=True)
class QuizResult:
    """Outcome of one finished quiz."""

    score: int
    total: int
    learned: bool


def _paths(ctx: typer.Context) -> Paths:
    paths = getattr(ctx.obj, "paths", None)
    assert isinstance(paths, Paths)
    return paths


def _fail(error: HackerBlogError, as_json: bool) -> None:
    emit_error(error, as_json)
    raise typer.Exit(int(error.code))


def _open_db(paths: Paths) -> sqlite3.Connection:
    if not paths.db.exists():
        raise HackerBlogError(ExitCode.CONFIG, f"No database at {paths.db}", "hackerblog init")
    conn = connect(paths.db)
    migrate(conn)
    return conn


def _require_site(conn: sqlite3.Connection, slug: str | None) -> None:
    if slug is None:
        return
    if conn.execute("SELECT 1 FROM sites WHERE slug = ?", (slug,)).fetchone() is None:
        raise HackerBlogError(ExitCode.NOT_FOUND, f"Unknown site {slug}", "hackerblog sites list")


def _ready_post(conn: sqlite3.Connection, post_id: int) -> tuple[str, str]:
    row = conn.execute("SELECT status, url FROM posts WHERE id = ?", (post_id,)).fetchone()
    if row is None:
        raise HackerBlogError(
            ExitCode.NOT_FOUND, f"Unknown post {post_id}", "hackerblog posts list"
        )
    if row[0] != "ready":
        raise HackerBlogError(
            ExitCode.NOT_FOUND,
            f"Post {post_id} is {row[0]}, not ready",
            f"hackerblog posts show {post_id}",
        )
    return str(row[0]), str(row[1])


def _artifact(conn: sqlite3.Connection, post_id: int, kind: str) -> Path:
    row = conn.execute(
        "SELECT path FROM artifacts WHERE post_id = ? AND kind = ?", (post_id, kind)
    ).fetchone()
    path = Path(row[0]) if row else None
    if path is None or not path.is_file():
        raise HackerBlogError(
            ExitCode.NOT_FOUND,
            f"Post {post_id} has no {kind} file",
            f"hackerblog regenerate {post_id} --stage render",
        )
    return path


def _load_lesson(conn: sqlite3.Connection, post_id: int) -> LessonSpec:
    path = _artifact(conn, post_id, "digest")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        lesson = LessonSpec.model_validate(document["lesson"])
    except (OSError, ValueError, KeyError, TypeError, ValidationError) as error:
        raise HackerBlogError(
            ExitCode.NOT_FOUND,
            f"No valid lesson in {path}: {type(error).__name__}",
            f"hackerblog regenerate {post_id} --stage lesson",
        ) from error
    assert len(lesson.final_quiz) >= 1
    return lesson


def search_command(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help="Words to look for.")],
    site: Annotated[str | None, typer.Option("--site", help="Only this site slug.")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=SEARCH_LIMIT_MAX)] = 10,
    as_json: JsonFlag = False,
) -> None:
    """Full-text search over titles, summaries, notes and post text."""
    try:
        conn = _open_db(_paths(ctx))
        _require_site(conn, site)
        hits = search(conn, query, site, limit)
    except HackerBlogError as error:
        _fail(error, as_json)
        return
    except ValueError as error:
        _fail(HackerBlogError(ExitCode.USAGE, str(error), "hackerblog search WORDS"), as_json)
        return
    if as_json:
        rows = [
            {
                "post_id": h.post_id,
                "site": clean(h.site),
                "title": clean(h.title),
                "snippet": clean(h.snippet),
            }
            for h in hits
        ]
        emit_json(rows)
        return
    for hit in hits:
        say(f"{hit.post_id:>5}  {hit.site}  {hit.title}")
        say(f"       {hit.snippet}")
    if not hits:
        say("No matches.")


def open_command(
    ctx: typer.Context,
    post_id: Annotated[int, typer.Argument(metavar="ID")],
    notes: Annotated[bool, typer.Option("--notes", help="Open notes.md.")] = False,
    source: Annotated[bool, typer.Option("--source", help="Open the original URL.")] = False,
    print_only: Annotated[bool, typer.Option("--print", help="Print, do not open.")] = False,
) -> None:
    """Open a post's lesson, notes or source, and mark it read."""
    if notes and source:
        _fail(HackerBlogError(ExitCode.USAGE, "--notes and --source exclude each other"), False)
    try:
        conn = _open_db(_paths(ctx))
        _, url = _ready_post(conn, post_id)
        if source and not url.startswith(("https://", "http://")):
            raise HackerBlogError(ExitCode.BLOCKED, f"Refusing to open {url}")
        target = url if source else str(_artifact(conn, post_id, "notes" if notes else "lesson"))
    except HackerBlogError as error:
        _fail(error, False)
        return
    if print_only:
        say(target)
    elif notes:
        typer.launch(target)
    elif not webbrowser.open(target if source else Path(target).as_uri()):
        _fail(HackerBlogError(ExitCode.FAILED, "No browser found", "Add --print"), False)
    conn.execute(
        "UPDATE posts SET reading_status = 'read' WHERE id = ? AND reading_status = 'unread'",
        (post_id,),
    )


def _show(
    prompt_item: Item, number: int, total: int, seed: int, write: Callable[[str], None]
) -> None:
    prompt = present(prompt_item, seed)
    write(clean(f"Question {number}/{total}: {prompt.text}"))
    for index, choice in enumerate(prompt.choices, start=1):
        write(clean(f"  {index}. {choice}"))
    for index, target in enumerate(prompt.targets):
        write(clean(f"  {LETTERS[index]}) {target}"))
    write(prompt.how)


def _ask_item(
    item: Item, seed: int, read: Callable[[str], str], write: Callable[[str], None]
) -> bool:
    for _ in range(ANSWER_TRIES_MAX):
        try:
            graded = grade(item, read("Answer"), seed)
        except AnswerFormatError as error:
            write(str(error))
            continue
        write("Correct." if graded.correct else clean(f"Not quite. Answer: {graded.expected}"))
        write(clean(item.explanation))
        return graded.correct
    write("No valid answer; counted as wrong.")
    return False


def play_quiz(
    conn: sqlite3.Connection,
    post_id: int,
    seed: int,
    read: Callable[[str], str],
    write: Callable[[str], None],
) -> QuizResult:
    """Run the post's final quiz with read and write, then record the score.

    An exception from read (such as KeyboardInterrupt) propagates before anything is stored.
    Returns the score, the item count and whether the post is now learned.
    """
    items = _load_lesson(conn, post_id).final_quiz
    score = 0
    for number, item in enumerate(items, start=1):
        _show(item, number, len(items), seed, write)
        score += _ask_item(item, seed, read, write)
        write("")
    learned = record_quiz(conn, post_id, score, len(items))
    write(f"Score: {score}/{len(items)}" + (" - marked learned" if learned else ""))
    assert 0 <= score <= len(items)
    assert not learned or score * 100 >= len(items) * 80
    return QuizResult(score, len(items), learned)


def quiz_command(ctx: typer.Context, post_id: Annotated[int, typer.Argument(metavar="ID")]) -> None:
    """Take the post's quiz in the terminal; 80 percent or more marks it learned."""
    try:
        conn = _open_db(_paths(ctx))
        _ready_post(conn, post_id)
        _load_lesson(conn, post_id)
    except HackerBlogError as error:
        _fail(error, False)
        return
    if not sys.stdin.isatty():
        usage = HackerBlogError(ExitCode.USAGE, "quiz needs an interactive terminal")
        _fail(usage, False)

    def read(text: str) -> str:
        return str(typer.prompt(text, default="", show_default=False))

    try:
        play_quiz(conn, post_id, secrets.randbelow(2**31), read, say)
    except (typer.Abort, KeyboardInterrupt, EOFError):
        sys.stderr.write("\nQuiz abandoned; nothing recorded.\n")
        raise typer.Exit(int(ExitCode.INTERRUPTED)) from None


def _llm_settings(paths: Paths) -> Any:
    config, problems = load_config(paths.config)
    if config is None or problems:
        raise HackerBlogError(ExitCode.CONFIG, f"{paths.config} is invalid", "hackerblog check")
    key_name = PROVIDER_KEYS[config.llm.provider] or "HACKERBLOG_LLM_API_KEY"
    api_key = lookup(key_name, read_secrets(paths.secrets), os.environ)
    try:
        module = importlib.import_module("hackerblog.core.llm.settings")
    except ModuleNotFoundError as error:
        raise HackerBlogError(ExitCode.INTERNAL, "ask is not available in this build") from error
    return module.LLMSettings.from_config(config.llm, api_key)


def _backend() -> tuple[Callable[..., Any], Callable[[str, str], Any]]:
    try:
        backend = importlib.import_module("hackerblog.core.llm.agno_backend")
        schemas = importlib.import_module("hackerblog.core.llm.schemas")
        return backend.answer, schemas.Turn
    except (ModuleNotFoundError, AttributeError) as error:
        raise HackerBlogError(ExitCode.INTERNAL, "ask is not available in this build") from error


def answer_sources(
    conn: sqlite3.Connection, answer: Answer, site: str | None
) -> list[dict[str, object]]:
    """List the posts an answer cites that are ready and, with site set, on that site.

    Returns {id, title, lesson} per cited post in citation order, without duplicates.
    """
    found: list[dict[str, object]] = []
    for post_id in dict.fromkeys(answer.post_ids):
        row = conn.execute(SOURCE_SQL, (post_id, site, site)).fetchone()
        if row is None:
            continue
        lesson = conn.execute(
            "SELECT path FROM artifacts WHERE post_id = ? AND kind = 'lesson'", (post_id,)
        ).fetchone()
        found.append(
            {
                "id": post_id,
                "title": clean(str(row[0] or "")),
                "lesson": clean(lesson[0]) if lesson else None,
            }
        )
    assert len(found) <= len(answer.post_ids)
    return found


def _print_answer(conn: sqlite3.Connection, answer: Answer, site: str | None) -> None:
    say(answer.text)
    sources = answer_sources(conn, answer, site)
    if sources:
        say("Sources:")
    for source in sources:
        say(f"  {source['id']}  {source['title']}  {source['lesson'] or ''}")


def ask_command(
    ctx: typer.Context,
    question: Annotated[str | None, typer.Argument(help="What you want to know.")] = None,
    post: Annotated[int | None, typer.Option("--post", help="Focus on one post id.")] = None,
    site: Annotated[str | None, typer.Option("--site", help="Only this site slug.")] = None,
    interactive: Annotated[bool, typer.Option("-i", "--interactive")] = False,
    as_json: JsonFlag = False,
) -> None:
    """Ask a question answered from your notes with read-only tools."""
    if interactive and as_json:
        _fail(HackerBlogError(ExitCode.USAGE, "--json does not work with -i"), False)
    if not interactive and not (question or "").strip():
        _fail(HackerBlogError(ExitCode.USAGE, "QUESTION is required", "hackerblog ask -i"), as_json)
    paths = _paths(ctx)
    try:
        conn = _open_db(paths)
        _require_site(conn, site)
        if post is not None:
            _ready_post(conn, post)
        settings = _llm_settings(paths)
        answer_fn, turn = _backend()
    except HackerBlogError as error:
        _fail(error, as_json)
        return
    tools = build_tools(conn, site)
    focus = f"About post {post}: " if post is not None else ""
    history: list[Any] = []
    pending = question
    for _ in range(ASK_TURNS_MAX):
        if pending is None:
            pending = str(typer.prompt("ask", default="", show_default=False)).strip()
            if pending in {"", "exit", "quit"}:
                return
        try:
            generated = asyncio.run(answer_fn(focus + pending, tools, settings, history))
        except HackerBlogError as error:
            _fail(error, as_json)
            return
        result = generated.value
        assert isinstance(result, Answer)
        if as_json:
            emit_json({"answer": clean(result.text), "sources": answer_sources(conn, result, site)})
            return
        _print_answer(conn, result, site)
        if not interactive:
            return
        history = [*history, turn("user", pending), turn("assistant", result.text)]
        history = history[-HISTORY_TURNS_MAX:]
        pending = None


def register(app: typer.Typer) -> None:
    """Add the study commands to app."""
    app.command("search")(search_command)
    app.command("quiz")(quiz_command)
    app.command("open")(open_command)
    app.command("ask")(ask_command)
