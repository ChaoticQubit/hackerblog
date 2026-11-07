import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from tests.samples import digest_data, lesson_data

from hackerblog.core.llm.cache import cache_key, digest_hash, get, put, record_usage
from hackerblog.core.llm.schemas import Generated, LessonSpec, PostDigest
from hackerblog.core.store.migrate import connect, migrate

DIGEST_PARTS = {
    "model": "claude-haiku-5-5",
    "prompt_version": "digest.v1",
    "content_hash": "abc123",
}


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "state.db")
    migrate(connection)
    yield connection
    connection.close()


def test_cache_key_is_stable_and_hex() -> None:
    key = cache_key("digest", **DIGEST_PARTS)
    assert key == cache_key("digest", **DIGEST_PARTS)
    assert len(key) == 64
    int(key, 16)


@pytest.mark.parametrize("name", sorted(DIGEST_PARTS))
def test_every_component_changes_the_key(name: str) -> None:
    changed = {**DIGEST_PARTS, name: DIGEST_PARTS[name] + "x"}
    assert cache_key("digest", **changed) != cache_key("digest", **DIGEST_PARTS)


def test_lesson_key_depends_on_digest_hash() -> None:
    first = cache_key("lesson", "m", "lesson.v1", "abc", digest_hash="d1")
    second = cache_key("lesson", "m", "lesson.v1", "abc", digest_hash="d2")
    assert first != second
    assert first != cache_key("digest", "m", "lesson.v1", "abc")


def test_lesson_key_requires_digest_hash_and_digest_forbids_it() -> None:
    with pytest.raises(AssertionError):
        cache_key("lesson", "m", "lesson.v1", "abc")
    with pytest.raises(AssertionError):
        cache_key("digest", "m", "digest.v1", "abc", digest_hash="d1")


def test_digest_hash_follows_content() -> None:
    digest = PostDigest.model_validate(digest_data())
    changed = digest.model_copy(update={"title": "Other"})
    assert digest_hash(digest) == digest_hash(PostDigest.model_validate(digest_data()))
    assert digest_hash(digest) != digest_hash(changed)


def test_miss_then_hit_costs_zero_tokens(conn: sqlite3.Connection) -> None:
    key = cache_key("digest", **DIGEST_PARTS)
    assert get(conn, key, PostDigest) is None
    digest = PostDigest.model_validate(digest_data())
    put(conn, key, "digest", Generated(digest, "claude-haiku-5-5", 1200, 800))
    hit = get(conn, key, PostDigest)
    assert hit is not None
    assert hit.value == digest
    assert hit.model == "claude-haiku-5-5"
    assert (hit.input_tokens, hit.output_tokens) == (0, 0)


def test_put_overwrites_and_stores_the_paid_tokens(conn: sqlite3.Connection) -> None:
    key = cache_key("lesson", "m", "lesson.v1", "abc", digest_hash="d")
    lesson = LessonSpec.model_validate(lesson_data())
    put(conn, key, "lesson", Generated(lesson, "m", 10, 20))
    put(conn, key, "lesson", Generated(lesson, "m", 30, 40))
    rows = conn.execute("SELECT input_tokens, output_tokens FROM llm_cache").fetchall()
    assert rows == [(30, 40)]
    hit = get(conn, key, LessonSpec)
    assert hit is not None
    assert hit.value == lesson


def test_get_with_wrong_schema_fails_loudly(conn: sqlite3.Connection) -> None:
    key = cache_key("digest", **DIGEST_PARTS)
    put(conn, key, "digest", Generated(PostDigest.model_validate(digest_data()), "m", 1, 1))
    with pytest.raises(AssertionError):
        get(conn, key, LessonSpec)


def test_record_usage_accumulates_per_day_model_stage(conn: sqlite3.Connection) -> None:
    day = date(2026, 11, 6)
    record_usage(conn, "digest", Generated("x", "m", 100, 50), day)
    record_usage(conn, "digest", Generated("y", "m", 10, 5), day)
    record_usage(conn, "ask", Generated("z", "m", 1, 1), day)
    rows = conn.execute(
        "SELECT day, model, stage, calls, input_tokens, output_tokens FROM llm_usage ORDER BY stage"
    ).fetchall()
    assert rows == [("2026-11-06", "m", "ask", 1, 1, 1), ("2026-11-06", "m", "digest", 2, 110, 55)]
