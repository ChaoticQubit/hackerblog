"""LLM output cache and token usage ledger over llm_cache and llm_usage (section 11.6).

A cache hit returns the stored output with zero tokens, because nothing was paid for it.
"""

import hashlib
import sqlite3
from datetime import UTC, date, datetime

from pydantic import BaseModel

from hackerblog.core.llm.schemas import SCHEMA_VERSION, Generated, LessonSpec, PostDigest, Stage

STAGE_SCHEMAS: dict[Stage, type[BaseModel]] = {"digest": PostDigest, "lesson": LessonSpec}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cache_key(
    stage: Stage,
    model: str,
    prompt_version: str,
    content_hash: str,
    digest_hash: str | None = None,
) -> str:
    """Return sha256(stage|model|prompt_version|SCHEMA_VERSION|content_hash[|digest_hash]).

    The lesson stage must pass the hash of the digest it was built from; digest must not.
    """
    assert model
    assert content_hash
    assert (stage == "lesson") == (digest_hash is not None)
    parts = [stage, model, prompt_version, SCHEMA_VERSION, content_hash]
    key = _sha256("|".join([*parts, digest_hash] if digest_hash is not None else parts))
    assert len(key) == 64
    return key


def digest_hash(digest: PostDigest) -> str:
    """Return a stable sha256 of a digest, for the lesson cache key."""
    text = digest.model_dump_json()
    assert text.startswith("{")
    return _sha256(text)


def get[T: BaseModel](conn: sqlite3.Connection, key: str, schema: type[T]) -> Generated[T] | None:
    """Return the cached output for key parsed as schema, at zero tokens; None on a miss."""
    assert len(key) == 64
    row = conn.execute("SELECT stage, model, output_json FROM llm_cache WHERE key = ?", (key,))
    found: tuple[str, str, str] | None = row.fetchone()
    if found is None:
        return None
    stage, model, output_json = found
    assert stage in STAGE_SCHEMAS
    assert STAGE_SCHEMAS[stage] is schema, f"cache row is {stage}, asked for {schema.__name__}"
    return Generated(schema.model_validate_json(output_json), model, 0, 0)


def put(conn: sqlite3.Connection, key: str, stage: Stage, generated: Generated[BaseModel]) -> None:
    """Store a generated output under key, replacing any older row."""
    assert len(key) == 64
    now = datetime.now(UTC).isoformat(timespec="seconds")
    conn.execute(
        "INSERT OR REPLACE INTO llm_cache"
        " (key, stage, model, output_json, input_tokens, output_tokens, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            key,
            stage,
            generated.model,
            generated.value.model_dump_json(),
            generated.input_tokens,
            generated.output_tokens,
            now,
        ),
    )
    assert conn.execute("SELECT 1 FROM llm_cache WHERE key = ?", (key,)).fetchone() is not None


def record_usage(
    conn: sqlite3.Connection, stage: str, generated: Generated[object], day: date
) -> None:
    """Add one call and its tokens to the llm_usage row of (day, model, stage)."""
    assert stage in {"digest", "lesson", "ask"}
    conn.execute(
        "INSERT INTO llm_usage (day, model, stage, calls, input_tokens, output_tokens)"
        " VALUES (?, ?, ?, 1, ?, ?)"
        " ON CONFLICT (day, model, stage) DO UPDATE SET calls = calls + 1,"
        " input_tokens = input_tokens + excluded.input_tokens,"
        " output_tokens = output_tokens + excluded.output_tokens",
        (day.isoformat(), generated.model, stage, generated.input_tokens, generated.output_tokens),
    )
    row = conn.execute(
        "SELECT calls FROM llm_usage WHERE day = ? AND model = ? AND stage = ?",
        (day.isoformat(), generated.model, stage),
    ).fetchone()
    assert row is not None
    assert row[0] >= 1
