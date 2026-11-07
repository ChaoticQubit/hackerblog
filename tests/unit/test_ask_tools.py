import inspect
import json
import sqlite3
from pathlib import Path

import pytest
from tests.unit.test_search import add_post, add_site, make_db

from hackerblog.core.llm.ask_tools import BODY_CHARS_MAX, build_tools


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    conn = make_db(tmp_path)
    alpha = add_site(conn, "alpha")
    beta = add_site(conn, "beta")
    add_post(conn, alpha, "Limit overrun races", body="race " * 3000, published_at="2026-11-02")
    add_post(
        conn, beta, "Heap \x1b[31mgrooming\x07", body="races in the heap", published_at="2026-11-03"
    )
    add_post(conn, beta, "Draft", status="queued")
    return conn


def tools_by_name(conn: sqlite3.Connection, site: str | None = None) -> dict[str, object]:
    return {tool.__name__: tool for tool in build_tools(conn, site)}


def test_three_documented_typed_tools(conn: sqlite3.Connection) -> None:
    tools = build_tools(conn)
    assert [tool.__name__ for tool in tools] == ["search_notes", "get_post", "list_recent"]
    for tool in tools:
        assert inspect.getdoc(tool)
        hints = inspect.get_annotations(tool, eval_str=True)
        assert hints["return"] is str
        assert len(hints) >= 2


def test_search_notes_returns_json_hits(conn: sqlite3.Connection) -> None:
    search_notes = build_tools(conn)[0]
    hits = json.loads(search_notes("races", 5))
    assert {hit["title"] for hit in hits} == {"Limit overrun races", "Heap grooming"}
    assert all("\x1b" not in hit["title"] for hit in hits)


def test_search_notes_honours_site_and_bounds(conn: sqlite3.Connection) -> None:
    search_notes = build_tools(conn, "alpha")[0]
    hits = json.loads(search_notes("races", 500))
    assert [hit["site"] for hit in hits] == ["alpha"]
    assert json.loads(search_notes('"NEAR(*', 0)) == []


def test_get_post_caps_body_and_includes_notes(conn: sqlite3.Connection) -> None:
    get_post = build_tools(conn)[1]
    post = json.loads(get_post(1))
    assert post["title"] == "Limit overrun races"
    assert post["notes_markdown"].startswith("## Race window")
    assert len(post["tldr"]) == 3
    assert len(post["body_md"]) <= BODY_CHARS_MAX
    assert BODY_CHARS_MAX == 4000
    assert post["body_truncated"] is True


def test_get_post_unknown_or_out_of_site(conn: sqlite3.Connection) -> None:
    assert json.loads(build_tools(conn)[1](999)) == {"error": "post 999 not found"}
    assert json.loads(build_tools(conn, "alpha")[1](2)) == {"error": "post 2 not found"}
    assert json.loads(build_tools(conn)[1](3)) == {"error": "post 3 not found"}


def test_get_post_strips_control_characters(conn: sqlite3.Connection) -> None:
    text = build_tools(conn)[1](2)
    assert "\x1b" not in json.loads(text)["title"]
    assert "\x07" not in json.loads(text)["title"]


def test_list_recent_newest_first_ready_only(conn: sqlite3.Connection) -> None:
    list_recent = build_tools(conn)[2]
    posts = json.loads(list_recent(None, 10))
    assert [post["id"] for post in posts] == [2, 1]
    assert json.loads(list_recent("alpha", 10))[0]["id"] == 1
    assert len(json.loads(list_recent(None, 1))) == 1


def test_list_recent_site_scope_cannot_be_widened(conn: sqlite3.Connection) -> None:
    list_recent = build_tools(conn, "alpha")[2]
    assert [post["id"] for post in json.loads(list_recent("beta", 10))] == []
    assert [post["id"] for post in json.loads(list_recent(None, 10))] == [1]


def test_tools_never_write(conn: sqlite3.Connection) -> None:
    before = conn.total_changes
    for name, tool in tools_by_name(conn).items():
        assert callable(tool), name
    search_notes, get_post, list_recent = build_tools(conn)
    search_notes("races", 5)
    get_post(1)
    list_recent(None, 5)
    assert conn.total_changes == before


def test_tools_connection_refuses_writes(conn: sqlite3.Connection) -> None:
    build_tools(conn)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("UPDATE posts SET status = 'failed'")
    assert conn.execute("SELECT count(*) FROM posts WHERE status = 'failed'").fetchone()[0] == 0
