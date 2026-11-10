import fcntl
import json
import os
import tomllib
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hackerblog.cli.main import app
from hackerblog.core.store import repo
from hackerblog.core.store.migrate import connect

runner = CliRunner()

OPML = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>Subscriptions</title></head>
  <body>
    <outline text="Security">
      <outline type="rss" text="Alpha" title="Alpha Blog"
               xmlUrl="https://alpha.example/feed.xml" htmlUrl="https://alpha.example/"/>
      <outline type="rss" text="Beta" xmlUrl="https://beta.example/rss"/>
      <outline type="rss" text="Bad" xmlUrl="ftp://bad.example/feed"/>
      <outline type="rss" text="Local" xmlUrl="http://127.0.0.1/feed"/>
    </outline>
  </body>
</opml>
"""


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    values = {
        "HOME": str(tmp_path),
        "HACKERBLOG_HOME": str(tmp_path / "hb"),
        "HACKERBLOG_CONFIG": "",
        "ANTHROPIC_API_KEY": "",
        "NO_COLOR": "1",
    }
    code, out = invoke(values, "init", "--output-dir", str(tmp_path / "out"))
    assert code == 0, out
    return values


def invoke(env: dict[str, str], *args: str) -> tuple[int, str]:
    result = runner.invoke(app, list(args), env=env)
    return result.exit_code, result.output


def config_path(env: dict[str, str]) -> Path:
    return Path(env["HACKERBLOG_HOME"]) / "sites.toml"


def sites_in(env: dict[str, str]) -> list[dict[str, object]]:
    data = tomllib.loads(config_path(env).read_text(encoding="utf-8"))
    return data.get("site", [])


def data_of(out: str) -> Any:
    document = json.loads(out)
    assert document["ok"] is True
    return document["data"]


def test_add_appends_a_site_block_and_keeps_the_rest(env: dict[str, str]) -> None:
    before = config_path(env).read_text(encoding="utf-8")
    code, out = invoke(
        env,
        "sites",
        "add",
        "https://blog.example.com/writing/",
        "--no-discover",
        "--tag",
        "web",
        "--tag",
        'say "hi"',
        "--backfill-limit",
        "0",
    )
    assert code == 0, out
    assert "Added site blog-example-com-writing" in out
    after = config_path(env).read_text(encoding="utf-8")
    assert after.startswith(before)
    assert sites_in(env) == [
        {
            "url": "https://blog.example.com/writing/",
            "tags": ["web", 'say "hi"'],
            "backfill_limit": 0,
        }
    ]


def test_add_rejects_duplicates_with_config_exit(env: dict[str, str]) -> None:
    assert invoke(env, "sites", "add", "https://a.example/", "--no-discover")[0] == 0
    code, out = invoke(env, "sites", "add", "https://A.example", "--no-discover")
    assert code == 3, out
    assert "already followed" in out
    assert len(sites_in(env)) == 1


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1/", "http://169.254.169.254/latest/", "http://[::1]/", "ftp://x.example/"],
)
def test_add_refuses_unsafe_urls_with_blocked_exit(env: dict[str, str], url: str) -> None:
    code, out = invoke(env, "sites", "add", url, "--no-discover", "--json")
    assert code == 9, out
    assert json.loads(out)["error"]["code"] == "blocked"
    assert sites_in(env) == []


def test_list_and_show_sites(env: dict[str, str]) -> None:
    invoke(env, "sites", "add", "https://a.example/blog/", "--no-discover", "--name", "A")
    code, out = invoke(env, "sites", "list", "--json")
    assert code == 0, out
    listed = data_of(out)
    assert [item["slug"] for item in listed] == ["a-example-blog"]
    code, out = invoke(env, "sites", "show", "a-example-blog", "--json")
    assert code == 0, out
    shown = data_of(out)
    assert shown["name"] == "A"
    assert shown["max_new_per_poll"] == 20
    assert shown["health"] == "ok"
    code, out = invoke(env, "sites", "show", "nope")
    assert code == 4, out


def test_import_opml_dry_run_then_real(env: dict[str, str], tmp_path: Path) -> None:
    opml = tmp_path / "subs.opml"
    opml.write_text(OPML, encoding="utf-8")
    code, out = invoke(env, "sites", "import-opml", str(opml), "--dry-run", "--json")
    assert code == 5, out
    preview = data_of(out)
    assert [item["url"] for item in preview["added"]] == [
        "https://alpha.example/",
        "https://beta.example/rss",
    ]
    assert len(preview["invalid"]) == 2
    assert sites_in(env) == []
    code, out = invoke(env, "sites", "import-opml", str(opml), "--tag", "imported")
    assert code == 5, out
    stored = sites_in(env)
    assert [site["url"] for site in stored] == [
        "https://alpha.example/",
        "https://beta.example/rss",
    ]
    assert stored[0]["feed"] == "https://alpha.example/feed.xml"
    assert stored[0]["name"] == "Alpha Blog"
    assert stored[0]["tags"] == ["imported"]
    code, out = invoke(env, "sites", "import-opml", str(opml), "--json")
    again = data_of(out)
    assert again["added"] == []
    assert len(again["existing"]) == 2


def test_posts_show_lists_the_post_flags(env: dict[str, str]) -> None:
    conn = connect(Path(env["HACKERBLOG_HOME"]) / "hackerblog.db")
    try:
        site_id = repo.ensure_adhoc_site(conn)
        url = "https://alpha.example/post"
        post_id, _ = repo.insert_manual_post(conn, site_id, url, url)
        repo.set_flags(conn, post_id, ["quiz_low_variety"])
    finally:
        conn.close()
    code, out = invoke(env, "posts", "show", str(post_id))
    assert code == 0, out
    assert "flags: quiz_low_variety" in out
    code, out = invoke(env, "posts", "show", str(post_id), "--json")
    assert data_of(out)["flags"] == ["quiz_low_variety"]


def test_posts_list_empty_and_show_unknown(env: dict[str, str]) -> None:
    code, out = invoke(env, "posts", "list", "--status", "any", "--json")
    assert code == 0, out
    assert data_of(out) == []
    code, out = invoke(env, "posts", "show", "999")
    assert code == 4
    assert "no post with id 999" in out
    code, out = invoke(env, "posts", "list", "--site", "nope")
    assert code == 4, out


def test_poll_without_sites_and_with_unknown_site(env: dict[str, str]) -> None:
    code, out = invoke(env, "poll", "--no-process", "--json")
    assert code == 0, out
    report = data_of(out)
    assert report["sites"] == []
    code, out = invoke(env, "poll", "--site", "nope")
    assert code == 4, out


def test_poll_exits_locked_when_another_poll_holds_the_lock(env: dict[str, str]) -> None:
    lock = Path(env["HACKERBLOG_HOME"]) / "locks" / "poll.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, out = invoke(env, "poll", "--no-process")
    finally:
        os.close(fd)
    assert code == 8, out
    assert "--wait" in out


def test_commands_report_a_broken_config(env: dict[str, str]) -> None:
    config_path(env).write_text("version = 2\n", encoding="utf-8")
    for args in (("sites", "list"), ("posts", "list"), ("poll",)):
        code, out = invoke(env, *args)
        assert code == 3, (args, out)
        assert "hackerblog check" in out


def test_import_opml_refuses_files_over_5_mib(env: dict[str, str], tmp_path: Path) -> None:
    opml = tmp_path / "huge.opml"
    opml.write_bytes(OPML.encode() + b" " * (5 * 1024 * 1024))
    code, out = invoke(env, "sites", "import-opml", str(opml))
    assert code == 2, out
    assert "larger than 5 MiB" in out
    assert sites_in(env) == []


def test_posts_list_shows_every_status_but_skipped_by_default(env: dict[str, str]) -> None:
    conn = connect(Path(env["HACKERBLOG_HOME"]) / "hackerblog.db")
    site_id = repo.ensure_adhoc_site(conn)
    for status in ("discovered", "queued", "ready", "failed", "skipped"):
        url = f"https://posts.example/{status}"
        post_id, _ = repo.insert_manual_post(conn, site_id, url, url)
        conn.execute("UPDATE posts SET status = ? WHERE id = ?", (status, post_id))
    conn.commit()
    conn.close()
    code, out = invoke(env, "posts", "list", "--json")
    assert code == 0, out
    assert sorted(post["status"] for post in data_of(out)) == [
        "discovered",
        "failed",
        "queued",
        "ready",
    ]
    code, out = invoke(env, "posts", "list", "--status", "skipped", "--json")
    assert [post["status"] for post in data_of(out)] == ["skipped"]
    code, out = invoke(env, "posts", "list", "--status", "any", "--json")
    assert len(data_of(out)) == 5


def test_add_writes_schedule_limit_and_aggregator(env: dict[str, str]) -> None:
    args = ["--schedule", "monthly", "--limit", "10", "--aggregator"]
    code, out = invoke(env, "sites", "add", "https://news.example/", "--no-discover", *args)
    assert code == 0, out
    assert sites_in(env) == [
        {"url": "https://news.example/", "schedule": "monthly", "limit": 10, "aggregator": True}
    ]
    code, out = invoke(env, "sites", "show", "news-example", "--json")
    assert code == 0, out
    shown = data_of(out)
    assert (shown["schedule"], shown["limit"], shown["aggregator"]) == ("monthly", 10, True)


@pytest.mark.parametrize("bad", [["--schedule", "fortnightly"], ["--limit", "0"]])
def test_add_rejects_bad_schedule_and_limit(env: dict[str, str], bad: list[str]) -> None:
    code, out = invoke(env, "sites", "add", "https://news.example/", "--no-discover", *bad)
    assert code in (2, 3), out
    assert sites_in(env) == []


EDITED = """
# my blogs
[[site]]
url = "https://a.example/"
name = "Alpha"  # keep me

[[site]]
url = "https://b.example/"
limit = 3
schedule = "hourly"

# trailing note
"""


def test_edit_changes_one_site_in_place_and_keeps_the_rest(env: dict[str, str]) -> None:
    path = config_path(env)
    before = path.read_text(encoding="utf-8") + EDITED
    path.write_text(before, encoding="utf-8")
    args = ["--schedule", "weekly", "--limit", "20", "--backfill-limit", "0", "--disable"]
    code, out = invoke(env, "sites", "edit", "b-example", *args)
    assert code == 0, out
    after = path.read_text(encoding="utf-8")
    head, _, block = after.partition('url = "https://b.example/"\n')
    assert head == before.partition('url = "https://b.example/"\n')[0]
    assert block == (
        'limit = 20\nschedule = "weekly"\nbackfill_limit = 0\nenabled = false\n\n# trailing note\n'
    )
    code, out = invoke(env, "sites", "edit", "b-example", "--no-limit", "--enable", "--aggregator")
    assert code == 0, out
    assert sites_in(env)[1] == {
        "url": "https://b.example/",
        "schedule": "weekly",
        "backfill_limit": 0,
        "enabled": True,
        "aggregator": True,
    }
    assert sites_in(env)[0] == {"url": "https://a.example/", "name": "Alpha"}


def test_edit_refuses_unknown_sites_and_bad_values(env: dict[str, str]) -> None:
    assert invoke(env, "sites", "add", "https://a.example/", "--no-discover")[0] == 0
    before = config_path(env).read_bytes()
    code, out = invoke(env, "sites", "edit", "nope", "--limit", "5")
    assert code == 4, out
    code, out = invoke(env, "sites", "edit", "a-example", "--schedule", "61 * * * *")
    assert code == 3, out
    assert "schedule" in out
    code, out = invoke(env, "sites", "edit", "a-example")
    assert code == 2, out
    assert config_path(env).read_bytes() == before
