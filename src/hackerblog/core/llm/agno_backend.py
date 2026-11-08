"""The only module that imports Agno: digest, lesson and ask calls (sections 3 and 11.2).

AGNO_TELEMETRY is forced off before Agno is imported, every Agent is built with
telemetry=False, and no Agno storage, memory, knowledge or AgentOS feature is used. Agno
swallows provider exceptions into an error RunOutput, so each model's ainvoke is wrapped to
keep the exception and map it to a HackerBlogError with the right exit code.
"""

import os

os.environ["AGNO_TELEMETRY"] = "false"

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, NoReturn, get_args

from agno.agent import Agent
from agno.exceptions import ModelAuthenticationError
from agno.models.anthropic import Claude
from agno.models.base import Model
from agno.models.message import Message
from agno.models.ollama import Ollama
from agno.models.openai import OpenAIChat
from agno.models.openai.like import OpenAILike
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from pydantic import BaseModel, ValidationError
from pydantic_core import ErrorDetails

from hackerblog.core.errors import ExitCode, HackerBlogError
from hackerblog.core.llm.schemas import (
    Answer,
    Generated,
    InvalidModelOutputError,
    LessonSpec,
    PostContext,
    PostDigest,
    Strict,
    Turn,
    lesson_problems,
    quiz_variety_problems,
)
from hackerblog.core.llm.settings import LLMSettings
from hackerblog.core.logging import redact

type PromptStage = Literal["digest", "lesson", "ask"]

PROMPTS_DIR = Path(__file__).parent / "prompts"
PROMPT_VERSIONS: Mapping[PromptStage, str] = {
    "digest": "digest.v1",
    "lesson": "lesson.v1",
    "ask": "ask.v1",
}
OUTPUT_TOKENS_MAX: Mapping[PromptStage, int] = {"digest": 4000, "lesson": 8000, "ask": 2000}
PROMPT_TOKENS_MAX = 8192
GENERATION_ATTEMPTS = 2
ASK_TOOL_CALLS_MAX = 8
ANSWER_CHARS_MAX = 8000
ANSWER_POST_IDS_MAX = 20
REJECTION_CHARS_MAX = 2000
AUTH_STATUS_CODES = frozenset({401, 403, 404})
POST_TAG = re.compile(r"<\s*(/?)\s*untrusted_post\b[^>]*>?", re.IGNORECASE)
CITATION = re.compile(r"\[post:(\d{1,12})\]")


def load_prompt(stage: PromptStage) -> str:
    """Return the instruction text of the current prompt version of stage."""
    text = (PROMPTS_DIR / f"{PROMPT_VERSIONS[stage]}.txt").read_text(encoding="utf-8")
    assert text.strip()
    assert "untrusted" in text
    return text


def neutralise(text: str) -> str:
    """Rewrite every opening or closing untrusted_post tag in text so it cannot end the block."""
    cleaned = POST_TAG.sub(lambda match: f"[{match.group(1)}untrusted_post]", text)
    assert POST_TAG.search(cleaned) is None
    return cleaned


def _post_block(ctx: PostContext, parts: Sequence[str]) -> str:
    header = [
        f"Title: {ctx.title}",
        f"URL: {ctx.url}",
        f"Site: {ctx.site_name}",
        f"Tags: {', '.join(ctx.site_tags) or 'none'}",
        f"Author: {ctx.author or 'unknown'}",
        f"Published: {ctx.published_at or 'unknown'}",
    ]
    if ctx.truncated:
        header.append("Note: only the first part of a long post is included.")
    inside = neutralise("\n".join([*header, "", *parts]))
    block = f"<untrusted_post>\n{inside}\n</untrusted_post>"
    assert block.count("</untrusted_post>") == 1
    assert block.endswith("</untrusted_post>")
    return block


def digest_input(ctx: PostContext) -> str:
    """Return the user message of the digest stage: the post inside its untrusted block."""
    return _post_block(ctx, [ctx.body_md])


def lesson_input(ctx: PostContext, digest: PostDigest) -> str:
    """Return the user message of the lesson stage: digest and post in one untrusted block."""
    return _post_block(
        ctx, ["Digest (JSON):", digest.model_dump_json(indent=1), "", "Post:", ctx.body_md]
    )


def ollama_context(settings: LLMSettings) -> int:
    """Return the Ollama num_ctx that fits any stage: post, digest, prompt and longest output.

    One value for every stage, so Ollama does not reload the model between digest and lesson.
    """
    context = (
        settings.max_input_tokens
        + OUTPUT_TOKENS_MAX["digest"]
        + max(OUTPUT_TOKENS_MAX.values())
        + PROMPT_TOKENS_MAX
    )
    assert context > settings.max_input_tokens + max(OUTPUT_TOKENS_MAX.values())
    return context


def _model(settings: LLMSettings, model_id: str, output_tokens_max: int) -> Model:
    key = settings.api_key.get_secret_value() if settings.api_key is not None else None
    timeout = float(settings.request_timeout_seconds)
    if settings.provider == "anthropic":
        assert key is not None
        return Claude(
            id=model_id,
            api_key=key,
            max_tokens=output_tokens_max,
            timeout=timeout,
            retries=0,
            client_params={"max_retries": 0},
        )
    if settings.provider == "openai":
        assert key is not None
        return OpenAIChat(
            id=model_id,
            api_key=key,
            max_completion_tokens=output_tokens_max,
            timeout=timeout,
            max_retries=0,
            retries=0,
        )
    assert settings.base_url is not None
    if settings.provider == "ollama":
        return Ollama(
            id=model_id,
            host=settings.base_url.rstrip("/").removesuffix("/v1"),
            api_key=None,
            options={"num_ctx": ollama_context(settings), "num_predict": output_tokens_max},
            timeout=timeout,
            retries=0,
        )
    return OpenAILike(
        id=model_id,
        api_key=key or "not-needed",
        base_url=settings.base_url,
        max_tokens=output_tokens_max,
        timeout=timeout,
        max_retries=0,
        retries=0,
    )


def _agno_method(target: object, name: str) -> Callable[..., Awaitable[Any]]:
    """Return an async Agno method by name, typed for strict pyright.

    Agno declares some parameters without annotations, so direct attribute access is
    partially unknown to pyright; going through getattr keeps one typed entry point.
    """
    method: Callable[..., Awaitable[Any]] = getattr(target, name)
    assert callable(method)
    assert name in {"ainvoke", "arun"}
    return method


def _record_errors(model: Model) -> list[BaseException]:
    """Wrap model.ainvoke so exceptions Agno later swallows are kept in the returned list."""
    errors: list[BaseException] = []
    inner = _agno_method(model, "ainvoke")

    async def ainvoke(*args: Any, **kwargs: Any) -> Any:
        try:
            return await inner(*args, **kwargs)
        except Exception as error:
            errors.append(error)
            raise

    model.ainvoke = ainvoke
    assert vars(model)["ainvoke"] is ainvoke
    assert not errors
    return errors


def _raise_provider_error(
    model_id: str, output: RunOutput, errors: list[BaseException]
) -> NoReturn:
    error = errors[-1] if errors else None
    status = getattr(error, "status_code", None)
    detail = redact(str(output.content if output.content is not None else error))[:500]
    if isinstance(error, ModelAuthenticationError) or status in AUTH_STATUS_CODES:
        raise HackerBlogError(
            ExitCode.LLM_AUTH,
            f"Provider refused model {model_id}: {detail}",
            "Check the API key in secrets.env and the model names in [llm]",
        )
    raise HackerBlogError(
        ExitCode.FAILED,
        f"Provider error from model {model_id}: {detail}",
        "The job is retried with backoff; see hackerblog jobs list",
    )


async def _run(
    agent: Agent, model_id: str, errors: list[BaseException], message: str | list[Message]
) -> RunOutput:
    errors.clear()
    output = await _agno_method(agent, "arun")(message, stream=False, add_history_to_context=False)
    assert isinstance(output, RunOutput)
    if output.status != RunStatus.completed:
        _raise_provider_error(model_id, output, errors)
    assert output.content is not None
    return output


def _tokens(output: RunOutput) -> tuple[int, int]:
    metrics = output.metrics
    if metrics is None:
        return 0, 0
    assert metrics.input_tokens >= 0
    assert metrics.output_tokens >= 0
    return metrics.input_tokens, metrics.output_tokens


def _schema_names() -> frozenset[str]:
    """Return every field name and quiz type tag of the LLM output models."""
    names: set[str] = set()
    pending: list[type[Strict]] = [Strict]
    while pending:
        model = pending.pop()
        pending.extend(model.__subclasses__())
        names.update(model.model_fields)
        type_field = model.model_fields.get("type")
        if type_field is not None:
            names.update(str(tag) for tag in get_args(type_field.annotation))
    assert {"final_quiz", "source_quote", "mcq"} <= names
    return frozenset(names)


SCHEMA_NAMES = _schema_names()


def _describe(error: ErrorDetails) -> str:
    """Describe one validation error with only schema names and indices, never model input.

    A loc segment that is not a field name or quiz tag becomes <extra>; the union tag
    message, which quotes the input tag, is replaced by a fixed text.
    """
    loc = ".".join(
        str(part) if isinstance(part, int) or part in SCHEMA_NAMES else "<extra>"
        for part in error["loc"]
    )
    message = "unknown quiz type" if error["type"] == "union_tag_invalid" else error["msg"]
    return f"{loc}: {message}"


def _parse[T: BaseModel](content: object, schema: type[T]) -> T | str:
    """Return content as schema, or why it is not one, without echoing model output."""
    if isinstance(content, schema):
        return content
    try:
        return schema.model_validate_json(content if isinstance(content, str) else "")
    except ValidationError as error:
        found = error.errors(include_url=False, include_input=False, include_context=False)
        assert found
        return "; ".join(_describe(e) for e in found[:20])


def _agent(
    settings: LLMSettings,
    stage: PromptStage,
    model_id: str,
    tools: Sequence[Callable[..., str]] = (),
) -> tuple[Agent, list[BaseException]]:
    assert stage == "ask" or not tools
    model = _model(settings, model_id, OUTPUT_TOKENS_MAX[stage])
    errors = _record_errors(model)
    agent = Agent(
        model=model,
        instructions=load_prompt(stage),
        output_schema=None if stage == "ask" else (PostDigest if stage == "digest" else LessonSpec),
        tools=list(tools),
        tool_call_limit=ASK_TOOL_CALLS_MAX,
        db=None,
        markdown=False,
        add_history_to_context=False,
        retries=0,
        telemetry=False,
    )
    assert agent.telemetry is False
    assert os.environ["AGNO_TELEMETRY"] == "false"
    assert agent.db is None
    return agent, errors


async def _generate[T: BaseModel](
    settings: LLMSettings,
    stage: Literal["digest", "lesson"],
    schema: type[T],
    prompt_input: str,
    problems_of: Callable[[T], list[str]],
    *,
    soft_problems_of: Callable[[T], list[str]] = lambda _: [],
) -> Generated[T]:
    model_id = settings.notes_model if stage == "digest" else settings.lesson_model
    agent, errors = _agent(settings, stage, model_id)
    input_tokens = output_tokens = 0
    message = prompt_input
    rejection = ""
    kept: T | None = None
    for _ in range(GENERATION_ATTEMPTS):
        output = await _run(agent, model_id, errors, message)
        used_in, used_out = _tokens(output)
        input_tokens, output_tokens = input_tokens + used_in, output_tokens + used_out
        parsed = _parse(output.content, schema)
        rejection = parsed if isinstance(parsed, str) else "; ".join(problems_of(parsed))
        if not isinstance(parsed, str) and not rejection:
            rejection = "; ".join(soft_problems_of(parsed))
            if not rejection or kept is not None:
                return Generated(parsed, model_id, input_tokens, output_tokens)
            kept = parsed
        reason = neutralise(rejection)[:REJECTION_CHARS_MAX]
        message = (
            f"{prompt_input}\n\nYour previous answer was rejected: {reason}\n"
            f"Return one corrected JSON object that matches the {schema.__name__} schema."
        )
    if kept is not None:
        return Generated(kept, model_id, input_tokens, output_tokens)
    assert rejection
    raise InvalidModelOutputError(
        f"{stage} output invalid after {GENERATION_ATTEMPTS} tries: {rejection}",
        Generated(None, model_id, input_tokens, output_tokens),
    )


async def generate_digest(ctx: PostContext, settings: LLMSettings) -> Generated[PostDigest]:
    """Run the digest stage on one post with notes_model; retries once on invalid output."""
    return await _generate(settings, "digest", PostDigest, digest_input(ctx), lambda _: [])


async def generate_lesson(
    ctx: PostContext, digest: PostDigest, settings: LLMSettings
) -> Generated[LessonSpec]:
    """Run the lesson stage with lesson_model; output must also pass lesson_problems.

    A lesson whose final quiz lacks type variety is retried once with the missing types
    named, then kept as it is; the pipeline flags it.
    """
    return await _generate(
        settings,
        "lesson",
        LessonSpec,
        lesson_input(ctx, digest),
        lesson_problems,
        soft_problems_of=quiz_variety_problems,
    )


async def answer(
    question: str,
    tools: Sequence[Callable[..., str]],
    settings: LLMSettings,
    history: Sequence[Turn] = (),
) -> Generated[Answer]:
    """Answer a question with read-only tools; post ids come from [post:ID] citations."""
    agent, errors = _agent(settings, "ask", settings.lesson_model, tools)
    messages = [Message(role=turn.role, content=turn.text) for turn in history]
    output = await _run(
        agent, settings.lesson_model, errors, [*messages, Message(role="user", content=question)]
    )
    text = str(output.content)
    post_ids = list(dict.fromkeys(int(found) for found in CITATION.findall(text)))
    value = Answer(text=text[:ANSWER_CHARS_MAX], post_ids=post_ids[:ANSWER_POST_IDS_MAX])
    assert len(value.post_ids) <= ANSWER_POST_IDS_MAX
    return Generated(value, settings.lesson_model, *_tokens(output))
