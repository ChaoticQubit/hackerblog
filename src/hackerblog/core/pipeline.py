"""Processing pipeline: one post from fetched page to a ready lesson (sections 9, 11, 12, 13).

Stages: fetch and extract, digest, lesson, grounding, render. Digest and lesson outputs are
cached by the hash of the exact text sent, so a re-run of unchanged content costs zero tokens.
run_worker claims process_post jobs and feeds them through process_post.
"""

import asyncio
import os
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel

from hackerblog.core import jobs
from hackerblog.core.config import load_config
from hackerblog.core.credentials import PROVIDER_KEYS, lookup, read_secrets
from hackerblog.core.errors import ExitCode, HackerBlogError
from hackerblog.core.extract import (
    CHARS_PER_TOKEN,
    MIN_WORDS,
    content_hash,
    extract,
    truncate,
)
from hackerblog.core.llm import cache
from hackerblog.core.llm.grounding import QUIZ_UNGROUNDED, GroundedLesson, ground_lesson
from hackerblog.core.llm.schemas import (
    Generated,
    LessonSpec,
    PostContext,
    PostDigest,
    quiz_variety_problems,
)
from hackerblog.core.llm.settings import LLMSettings
from hackerblog.core.logging import redact
from hackerblog.core.paths import Paths
from hackerblog.core.render.files import post_dir, write_atomic
from hackerblog.core.render.index import IndexEntry, render_index
from hackerblog.core.render.lesson import TEMPLATE_VERSION, PostMeta, post_json, render_lesson
from hackerblog.core.render.notes import render_notes
from hackerblog.core.store import repo

Stage = Literal["all", "lesson", "render"]
Status = Literal["ready", "skipped"]
PROMPT_VERSIONS = {"digest": "digest.v1", "lesson": "lesson.v1"}
OUTPUT_TOKENS_MAX = {"digest": 4000, "lesson": 8000}
WORDS_PER_MINUTE = 200
HTTP_OK = 200
QUIZ_LOW_VARIETY = "quiz_low_variety"


class Page(Protocol):
    """The part of a fetched page the pipeline reads."""

    @property
    def url(self) -> str: ...
    @property
    def status(self) -> int: ...
    @property
    def body(self) -> bytes: ...


class PageFetcher(Protocol):
    """Anything with net.fetcher.Fetcher's get: SSRF-guarded, robots-aware, size-capped."""

    async def get(self, url: str, *, kind: Literal["page"]) -> Page: ...


class Backend(Protocol):
    """The two generation calls of core.llm.agno_backend."""

    async def generate_digest(
        self, ctx: PostContext, settings: LLMSettings
    ) -> Generated[PostDigest]: ...

    async def generate_lesson(
        self, ctx: PostContext, digest: PostDigest, settings: LLMSettings
    ) -> Generated[LessonSpec]: ...


@dataclass(frozen=True)
class Settings:
    """Everything processing needs from sites.toml and secrets."""

    llm: LLMSettings | None
    max_input_tokens: int
    output_dir: Path
    daily_token_budget: int | None
    site_tags: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        assert self.daily_token_budget is None or self.daily_token_budget > 0
        assert self.max_input_tokens > 0
        assert self.llm is None or self.llm.max_input_tokens == self.max_input_tokens
        assert self.output_dir.is_absolute(), "output_dir must be absolute"


@dataclass(frozen=True)
class Outcome:
    """How one post ended: ready or skipped, the new tokens spent, and the skip reason."""

    post_id: int
    status: Status
    tokens: int
    reason: str | None = None

    def __post_init__(self) -> None:
        assert self.tokens >= 0
        assert (self.status == "skipped") == (self.reason is not None)


class BudgetExhaustedError(Exception):
    """The daily token budget would be crossed; the post waits until `until`."""

    def __init__(self, until: datetime) -> None:
        super().__init__(f"daily token budget reached until {until.isoformat()}")
        self.until = until


class SkipPostError(Exception):
    """The post is not worth processing; reason goes to posts.skip_reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _noop() -> None:
    return None


def next_local_midnight(now: datetime) -> datetime:
    """Return the next local midnight after the aware datetime now, as a local datetime."""
    assert now.tzinfo is not None, "naive datetime"
    local = now.astimezone()
    midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    assert midnight > now
    return midnight


def _tokens_today(conn: sqlite3.Connection, today: date) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) FROM llm_usage WHERE day = ?",
        (today.isoformat(),),
    ).fetchone()
    return int(row[0])


def _check_budget(conn: sqlite3.Connection, settings: Settings, text: str, stage: str) -> None:
    if settings.daily_token_budget is None:
        return
    now = datetime.now().astimezone()
    estimate = len(text) // CHARS_PER_TOKEN + OUTPUT_TOKENS_MAX[stage]
    if _tokens_today(conn, now.date()) + estimate > settings.daily_token_budget:
        raise BudgetExhaustedError(next_local_midnight(now))


def _require_post(conn: sqlite3.Connection, post_id: int) -> repo.PostRow:
    post = repo.get_post(conn, post_id)
    if post is None:
        raise HackerBlogError(ExitCode.NOT_FOUND, f"no post {post_id}", "hackerblog posts list")
    return post


async def _fetch(conn: sqlite3.Connection, post: repo.PostRow, fetcher: PageFetcher) -> None:
    repo.set_post_status(conn, post.id, "fetching")
    page = await fetcher.get(post.url, kind="page")
    if page.status != HTTP_OK:
        raise HackerBlogError(
            ExitCode.FAILED, f"{post.url} answered HTTP {page.status}", "hackerblog jobs list"
        )
    extracted = await asyncio.to_thread(extract, page.body, page.url)
    if extracted.word_count < MIN_WORDS:
        raise SkipPostError("too_short")
    twin = repo.find_ready_by_hash(conn, extracted.content_hash)
    if twin is not None and twin.id != post.id:
        raise SkipPostError("duplicate")
    repo.save_extracted(conn, post.id, extracted, truncated=False)


def _site_name(conn: sqlite3.Connection, site_id: int) -> str:
    row = conn.execute("SELECT name FROM sites WHERE id = ?", (site_id,)).fetchone()
    assert row is not None, f"no site {site_id}"
    return str(row[0])


def _context(conn: sqlite3.Connection, post: repo.PostRow, settings: Settings) -> PostContext:
    if post.body_md is None:
        raise HackerBlogError(
            ExitCode.NOT_FOUND,
            f"post {post.id} has no extracted text",
            f"hackerblog process {post.url}",
        )
    body, truncated = truncate(post.body_md, settings.max_input_tokens)
    if truncated != post.truncated:
        conn.execute("UPDATE posts SET truncated = ? WHERE id = ?", (int(truncated), post.id))
    return PostContext(
        post_id=post.id,
        title=post.title or post.url,
        url=post.url,
        site_name=_site_name(conn, post.site_id),
        site_tags=tuple(settings.site_tags.get(post.site_slug, ())),
        author=post.author,
        published_at=post.published_at,
        body_md=body,
        truncated=truncated,
        content_hash=content_hash(body),
    )


async def _generate[T: BaseModel](
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    stage: Literal["digest", "lesson"],
    key: str,
    schema: type[T],
    budget_text: str,
    call: Callable[[], object],
    heartbeat: Callable[[], None],
    use_cache: bool,
) -> tuple[Generated[T], int]:
    if use_cache:
        hit = cache.get(conn, key, schema)
        if hit is not None:
            return hit, 0
    _check_budget(conn, settings, budget_text, stage)
    heartbeat()
    pending = call()
    assert asyncio.iscoroutine(pending)
    generated: Generated[T] = await pending
    assert isinstance(generated.value, schema)
    cache.put(conn, key, stage, generated)
    cache.record_usage(conn, stage, generated, datetime.now().astimezone().date())
    return generated, generated.input_tokens + generated.output_tokens


def _artifact(conn: sqlite3.Connection, post_id: int, kind: str) -> repo.ArtifactRow | None:
    return next((a for a in repo.artifacts_for(conn, post_id) if a.kind == kind), None)


def _stored_digest(conn: sqlite3.Connection, post: repo.PostRow) -> Generated[PostDigest]:
    stored = _artifact(conn, post.id, "digest")
    if post.digest_json is None or stored is None or stored.model is None:
        raise HackerBlogError(
            ExitCode.NOT_FOUND,
            f"post {post.id} has no stored digest",
            f"hackerblog regenerate {post.id} --stage all",
        )
    return Generated(PostDigest.model_validate_json(post.digest_json), stored.model, 0, 0)


def _stored_lesson(
    conn: sqlite3.Connection, post: repo.PostRow, ctx: PostContext, digest: PostDigest
) -> Generated[LessonSpec]:
    stored = _artifact(conn, post.id, "lesson")
    hit = None
    if stored is not None and stored.model is not None and stored.prompt_version is not None:
        key = cache.cache_key(
            "lesson",
            stored.model,
            stored.prompt_version,
            ctx.content_hash,
            cache.digest_hash(digest),
        )
        hit = cache.get(conn, key, LessonSpec)
    if hit is None:
        raise HackerBlogError(
            ExitCode.NOT_FOUND,
            f"post {post.id} has no stored lesson",
            f"hackerblog regenerate {post.id} --stage lesson",
        )
    return hit


def _post_folder(
    conn: sqlite3.Connection, post: repo.PostRow, settings: Settings, title: str
) -> Path:
    root = settings.output_dir.resolve()
    stored = _artifact(conn, post.id, "lesson")
    if stored is not None:
        folder = Path(stored.path).parent
        if folder.is_dir() and not folder.is_symlink() and folder.parent.parent == root:
            return folder
    day = (post.published_at or post.discovered_at)[:10]
    return post_dir(root, post.site_slug, day, title, post.id)


def _write(path: Path, text: str) -> tuple[str, int]:
    data = text.encode("utf-8")
    write_atomic(path, data)
    return sha256(data).hexdigest(), len(data)


def write_index(conn: sqlite3.Connection, output_dir: Path) -> Path:
    """Rewrite <output_dir>/index.html from every ready post that has a lesson on disk."""
    root = output_dir.resolve()
    names = dict(conn.execute("SELECT id, name FROM sites").fetchall())
    entries: list[IndexEntry] = []
    for post in repo.ready_posts(conn):
        lesson = _artifact(conn, post.id, "lesson")
        if lesson is None:
            continue
        folder = Path(lesson.path).parent
        if folder.parent.parent != root:
            continue
        rel = f"{post.site_slug}/{folder.name}"
        entries.append(
            IndexEntry(
                site_slug=post.site_slug,
                site_name=str(names[post.site_id]),
                title=post.title or post.url,
                published=(post.published_at or post.discovered_at)[:10],
                lesson_href=f"{rel}/lesson.html",
                notes_href=f"{rel}/notes.md",
                reading_status=post.reading_status,
            )
        )
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / "index.html"
    write_atomic(path, render_index(entries))
    return path


def _render(
    conn: sqlite3.Connection,
    post: repo.PostRow,
    settings: Settings,
    *,
    digest: Generated[PostDigest],
    lesson: Generated[LessonSpec],
    grounded: GroundedLesson,
    tokens: Mapping[str, Mapping[str, int]],
    update_index: bool,
) -> None:
    repo.set_post_status(conn, post.id, "rendering")
    variety = {QUIZ_LOW_VARIETY} if quiz_variety_problems(grounded.lesson) else set[str]()
    stale = {QUIZ_UNGROUNDED, QUIZ_LOW_VARIETY}
    flags = tuple(sorted({*post.flags} - stale | {*grounded.flags} | variety))
    repo.set_flags(conn, post.id, flags)
    title = post.title or digest.value.title
    meta = PostMeta(
        post_id=post.id,
        title=title,
        site_slug=post.site_slug,
        site_name=_site_name(conn, post.site_id),
        author=post.author,
        published_at=post.published_at,
        source_url=post.url,
        reading_minutes=max(1, -(-(post.word_count or 0) // WORDS_PER_MINUTE)),
        flags=flags,
        truncated=post.truncated,
        models=tuple(dict.fromkeys((digest.model, lesson.model))),
        prompt_versions=PROMPT_VERSIONS,
        generated_at=repo.utc_now(),
    )
    folder = _post_folder(conn, post, settings, title)
    generation = {
        "models": {"digest": digest.model, "lesson": lesson.model},
        "prompt_versions": PROMPT_VERSIONS,
        "tokens": tokens,
    }
    written = {
        "lesson": (folder / "lesson.html", render_lesson(meta, digest.value, grounded.lesson)),
        "notes": (folder / "notes.md", render_notes(meta, digest.value, "lesson.html")),
        "digest": (
            folder / "post.json",
            post_json(meta, digest.value, grounded.lesson, generation),
        ),
    }
    models = {"lesson": lesson.model, "notes": digest.model, "digest": digest.model}
    prompts = {"lesson": "lesson", "notes": "digest", "digest": "digest"}
    for kind, (path, text) in written.items():
        digest_sha, size = _write(path, text)
        repo.record_artifact(
            conn,
            post.id,
            kind,
            path=str(path),
            sha256=digest_sha,
            size=size,
            model=models[kind],
            prompt_version=PROMPT_VERSIONS[prompts[kind]],
            template_version=TEMPLATE_VERSION,
        )
    repo.mark_ready(
        conn,
        post.id,
        title=title,
        summary=digest.value.summary,
        notes=digest.value.notes_markdown,
        body=post.body_md or "",
    )
    if update_index:
        write_index(conn, settings.output_dir)


async def process_post(
    conn: sqlite3.Connection,
    post_id: int,
    settings: Settings,
    backend: Backend | None,
    *,
    fetcher: PageFetcher | None = None,
    stage: Stage = "all",
    force: bool = False,
    heartbeat: Callable[[], None] = _noop,
    update_index: bool = True,
) -> Outcome:
    """Process one post and return how it ended.

    With a fetcher the page is fetched and extracted first; without one the stored text is
    used. stage 'lesson' reruns only the lesson, 'render' only re-renders at zero tokens.
    force bypasses the cache. heartbeat runs before the fetch and every LLM call. backend may
    be None only for stage 'render'. update_index=False leaves index.html to the caller.
    Raises BudgetExhaustedError, HackerBlogError, or the backend's errors.
    """
    assert stage in {"all", "lesson", "render"}
    assert fetcher is None or stage == "all", "only stage all fetches"
    assert backend is not None or stage == "render", "only stage render runs without a backend"
    try:
        return await _process(
            conn,
            post_id,
            settings,
            backend,
            fetcher=fetcher,
            stage=stage,
            force=force,
            heartbeat=heartbeat,
            update_index=update_index,
        )
    except BudgetExhaustedError:
        repo.set_post_status(conn, post_id, "queued", error="daily token budget reached")
        raise


async def _process(
    conn: sqlite3.Connection,
    post_id: int,
    settings: Settings,
    backend: Backend | None,
    *,
    fetcher: PageFetcher | None,
    stage: Stage,
    force: bool,
    heartbeat: Callable[[], None],
    update_index: bool,
) -> Outcome:
    if fetcher is not None:
        heartbeat()
        try:
            await _fetch(conn, _require_post(conn, post_id), fetcher)
        except SkipPostError as skip:
            repo.set_post_status(conn, post_id, "skipped", skip_reason=skip.reason)
            return Outcome(post_id, "skipped", 0, skip.reason)
    post = _require_post(conn, post_id)
    ctx = _context(conn, post, settings)
    llm = settings.llm
    generator = backend
    assert llm is not None or stage == "render", "stages that call the LLM need its settings"
    digest_tokens = 0
    if stage == "all":
        assert generator is not None and llm is not None
        repo.set_post_status(conn, post_id, "generating")
        digest, digest_tokens = await _generate(
            conn,
            settings,
            stage="digest",
            key=cache.cache_key(
                "digest", llm.notes_model, PROMPT_VERSIONS["digest"], ctx.content_hash
            ),
            schema=PostDigest,
            budget_text=ctx.body_md,
            call=lambda: generator.generate_digest(ctx, llm),
            heartbeat=heartbeat,
            use_cache=not force,
        )
        repo.save_digest(conn, post_id, digest.value.model_dump_json())
    else:
        digest = _stored_digest(conn, post)

    async def make_lesson(use_cache: bool) -> tuple[Generated[LessonSpec], int]:
        lesson_backend, lesson_llm = generator, llm
        assert lesson_backend is not None and lesson_llm is not None
        return await _generate(
            conn,
            settings,
            stage="lesson",
            key=cache.cache_key(
                "lesson",
                lesson_llm.lesson_model,
                PROMPT_VERSIONS["lesson"],
                ctx.content_hash,
                cache.digest_hash(digest.value),
            ),
            schema=LessonSpec,
            budget_text=ctx.body_md + digest.value.model_dump_json(),
            call=lambda: lesson_backend.generate_lesson(ctx, digest.value, lesson_llm),
            heartbeat=heartbeat,
            use_cache=use_cache,
        )

    lesson_tokens = 0
    if stage == "render":
        lesson = _stored_lesson(conn, post, ctx, digest.value)
    else:
        repo.set_post_status(conn, post_id, "generating")
        lesson, lesson_tokens = await make_lesson(not force and stage == "all")
        if lesson_tokens > 0 and not ground_lesson(lesson.value, ctx.body_md).quiz_grounded:
            lesson, retry_tokens = await make_lesson(False)
            lesson_tokens += retry_tokens
    grounded = ground_lesson(lesson.value, ctx.body_md)
    heartbeat()
    tokens = {
        "digest": {"input": digest.input_tokens, "output": digest.output_tokens},
        "lesson": {"input": lesson.input_tokens, "output": lesson.output_tokens},
    }
    _render(
        conn,
        _require_post(conn, post_id),
        settings,
        digest=digest,
        lesson=lesson,
        grounded=grounded,
        tokens=tokens,
        update_index=update_index,
    )
    return Outcome(post_id, "ready", digest_tokens + lesson_tokens)


JobState = Literal["ready", "skipped", "failed", "dead", "deferred", "lost"]
WORKER_IDLE_S = 10.0
CONCURRENCY_MAX = 8
ERROR_CHARS_MAX = 500


@dataclass(frozen=True)
class JobResult:
    """How one claimed job ended, for progress lines and the summary."""

    job_id: int
    post_id: int
    state: JobState
    tokens: int
    detail: str | None = None


@dataclass
class WorkReport:
    """Every job result of one worker run, and the error that stopped it early, if any."""

    results: list[JobResult]
    stop: HackerBlogError | None = None

    @property
    def tokens(self) -> int:
        """New tokens spent by this run."""
        return sum(result.tokens for result in self.results)


def _utc() -> datetime:
    return datetime.now().astimezone()


def _error_text(error: BaseException) -> str:
    text = redact(str(error) or type(error).__name__)
    return text[:ERROR_CHARS_MAX]


async def run_job(
    conn: sqlite3.Connection,
    job: jobs.Job,
    settings: Settings,
    backend: Backend,
    fetcher: PageFetcher | None,
) -> JobResult:
    """Run one claimed process_post job and settle it: done, failed, dead or deferred.

    Raises HackerBlogError (after releasing the job) when the environment, not the post, is
    at fault: a rejected provider key, or a page fetch this build cannot make.
    """
    assert job.kind == "process_post" and job.post_id is not None
    assert job.state == "running"
    post_id = job.post_id
    if job.stage == "all" and fetcher is None:
        jobs.release(conn, job, _utc())
        raise HackerBlogError(
            ExitCode.INTERNAL, "page fetching is not available in this build", "hackerblog -vv"
        )
    try:
        outcome = await process_post(
            conn,
            post_id,
            settings,
            backend,
            fetcher=fetcher if job.stage == "all" else None,
            stage=_job_stage(job.stage),
            force=job.stage == "lesson",
            heartbeat=lambda: jobs.renew_lease(conn, job, _utc()),
        )
    except jobs.LeaseLostError:
        return JobResult(job.id, post_id, "lost", 0, "lease lost to another worker")
    except BudgetExhaustedError as stop:
        jobs.defer(conn, job, stop.until, _utc())
        return JobResult(job.id, post_id, "deferred", 0, str(stop))
    except HackerBlogError as error:
        if error.code == ExitCode.LLM_AUTH:
            jobs.release(conn, job, _utc())
            repo.set_post_status(conn, post_id, "queued", error=_error_text(error))
            raise
        if error.code == ExitCode.BLOCKED:
            repo.set_post_status(conn, post_id, "skipped", skip_reason="blocked")
            jobs.complete(conn, job, _utc())
            return JobResult(job.id, post_id, "skipped", 0, _error_text(error))
        return _failed(conn, job, error)
    except (ValueError, OSError) as error:
        return _failed(conn, job, error)
    except BaseException:
        jobs.release(conn, job, _utc())
        raise
    jobs.complete(conn, job, _utc())
    return JobResult(job.id, post_id, outcome.status, outcome.tokens, outcome.reason)


def _job_stage(stage: str) -> Stage:
    assert stage in {"all", "lesson", "render"}, f"unknown stage {stage!r}"
    return "render" if stage == "render" else "lesson" if stage == "lesson" else "all"


def _failed(conn: sqlite3.Connection, job: jobs.Job, error: BaseException) -> JobResult:
    assert job.post_id is not None
    text = _error_text(error)
    state = jobs.fail(conn, job, text, _utc())
    assert state in {"pending", "dead"}
    status: JobState = "dead" if state == "dead" else "failed"
    repo.set_post_status(conn, job.post_id, status, error=text)
    return JobResult(job.id, job.post_id, status, 0, text)


async def run_worker(
    conn: sqlite3.Connection,
    settings: Settings,
    backend: Backend,
    *,
    fetcher: PageFetcher | None,
    max_jobs: int | None = None,
    concurrency: int = 1,
    forever: bool = False,
    on_result: Callable[[JobResult], None] | None = None,
) -> WorkReport:
    """Claim and run process_post jobs until the queue is empty, max_jobs ran, or a stop.

    A budget stop defers the job and ends the run with a BUDGET error in report.stop; a
    rejected key or a missing fetcher ends it with that error. forever polls an empty queue
    every WORKER_IDLE_S seconds instead of returning.
    """
    assert 1 <= concurrency <= CONCURRENCY_MAX
    assert max_jobs is None or max_jobs >= 1
    report = WorkReport([])
    claimed = 0

    async def lane() -> None:
        nonlocal claimed
        while report.stop is None and (max_jobs is None or claimed < max_jobs):
            job = jobs.claim(conn, _utc())
            if job is None:
                if not forever:
                    return
                await asyncio.sleep(WORKER_IDLE_S)
                continue
            claimed += 1
            try:
                result = await run_job(conn, job, settings, backend, fetcher)
            except HackerBlogError as error:
                report.stop = error
                return
            report.results.append(result)
            if on_result is not None:
                on_result(result)
            if result.state == "deferred":
                report.stop = HackerBlogError(
                    ExitCode.BUDGET,
                    result.detail or "daily token budget reached",
                    "Jobs stay queued; they continue after local midnight",
                )

    await asyncio.gather(*(lane() for _ in range(concurrency)))
    assert max_jobs is None or len(report.results) <= max_jobs
    return report


def load_settings(
    paths: Paths, *, need_llm: bool, env: Mapping[str, str] | None = None
) -> Settings:
    """Build Settings from sites.toml and secrets.env; LLM settings only when need_llm.

    Raises HackerBlogError CONFIG for an invalid config, LLM_AUTH for a missing key.
    """
    config, problems = load_config(paths.config)
    if config is None or problems:
        raise HackerBlogError(ExitCode.CONFIG, f"{paths.config} is invalid", "hackerblog check")
    llm = None
    if need_llm:
        key_name = PROVIDER_KEYS[config.llm.provider] or "HACKERBLOG_LLM_API_KEY"
        values = os.environ if env is None else env
        api_key = lookup(key_name, read_secrets(paths.secrets), values)
        llm = LLMSettings.from_config(config.llm, api_key)
    tags = {site.slug: tuple(site.tags) for site in config.sites if site.slug}
    assert len(tags) == len(config.sites)
    return Settings(
        llm=llm,
        max_input_tokens=config.llm.max_input_tokens,
        output_dir=config.output.dir.resolve(),
        daily_token_budget=config.llm.daily_token_budget,
        site_tags=tags,
    )


def as_error(error: BaseException) -> HackerBlogError | None:
    """Map a processing failure to the HackerBlogError the CLI reports, or None if unexpected."""
    if isinstance(error, HackerBlogError):
        return error
    if isinstance(error, BudgetExhaustedError):
        until = error.until.strftime("%H:%M")
        return HackerBlogError(
            ExitCode.BUDGET, f"daily token budget reached until {until}", "hackerblog status"
        )
    if isinstance(error, ValueError | OSError):
        return HackerBlogError(ExitCode.FAILED, _error_text(error), "hackerblog -vv")
    return None


async def rerun(
    conn: sqlite3.Connection,
    post_id: int,
    settings: Settings,
    backend: Backend | None,
    *,
    stage: Stage,
    update_index: bool = True,
) -> Outcome:
    """Regenerate or re-render a post now, outside the job queue (regenerate and render).

    stage 'all' bypasses the cache. Refuses a post with an open job. On failure the post
    gets back its earlier status with the error recorded; raises HackerBlogError.
    """
    post = _require_post(conn, post_id)
    open_job = conn.execute(
        "SELECT id FROM jobs WHERE post_id = ? AND state IN ('pending', 'running')", (post_id,)
    ).fetchone()
    if open_job is not None:
        raise HackerBlogError(
            ExitCode.USAGE,
            f"post {post_id} has job {open_job[0]} waiting",
            "hackerblog work, or hackerblog jobs cancel " + str(open_job[0]),
        )
    try:
        return await process_post(
            conn,
            post_id,
            settings,
            backend,
            stage=stage,
            force=stage == "all",
            update_index=update_index,
        )
    except Exception as failure:
        error = as_error(failure)
        if error is None:
            raise
        repo.set_post_status(conn, post_id, post.status, post.skip_reason, error.message)
        raise error from failure
