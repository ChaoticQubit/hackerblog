import asyncio
import json
import sqlite3
import stat
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from functools import partial
from http.client import HTTPConnection
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit

import pytest
from tests.e2e.test_lesson_browser import A11Y_SCRIPT, CHROME, chrome, open_page, problems
from tests.integration.processing.live import live_settings
from tests.samples import RACE_POST_HTML

from hackerblog.core import pipeline
from hackerblog.core.llm import agno_backend
from hackerblog.core.llm.schemas import LessonSpec, PostDigest
from hackerblog.core.store import repo
from hackerblog.core.store.migrate import connect, migrate

pytestmark = [
    pytest.mark.integration,
    pytest.mark.live,
    pytest.mark.skipif(CHROME is None, reason="Google Chrome is not installed"),
]
LOG = Path(__file__).resolve().parents[3] / ".local-state" / "m2-acceptance.log"
__all__ = ["chrome"]


@dataclass(frozen=True)
class Fetched:
    url: str
    status: int
    body: bytes


class LocalPages:
    """Real HTTP GETs to the local fixture server, standing in for net.fetcher.Fetcher."""

    async def get(self, url: str, *, kind: Literal["page"]) -> Fetched:
        assert url.startswith("http://127.0.0.1:")
        return await asyncio.to_thread(self._get, url)

    @staticmethod
    def _get(url: str) -> Fetched:
        parts = urlsplit(url)
        connection = HTTPConnection(parts.hostname or "", parts.port or 80, timeout=10)
        try:
            connection.request("GET", parts.path)
            response = connection.getresponse()
            return Fetched(url, response.status, response.read())
        finally:
            connection.close()


@pytest.fixture(scope="module")
def blog() -> Iterator[str]:
    handler = partial(SimpleHTTPRequestHandler, directory=str(RACE_POST_HTML.parent))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/{RACE_POST_HTML.name}"
    server.shutdown()
    thread.join()


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = connect(tmp_path / "hb.db")
    migrate(db)
    yield db
    db.close()


def usage(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
    return conn.execute("SELECT * FROM llm_usage ORDER BY stage").fetchall()


def report(lines: list[str], label: str, value: object) -> None:
    line = f"[m2] {label}: {value}"
    print(line)
    lines.append(line)


def test_fixture_post_becomes_an_offline_accessible_lesson_and_rerun_is_free(
    conn: sqlite3.Connection, tmp_path: Path, blog: str, chrome: int
) -> None:
    llm = live_settings()
    settings = pipeline.Settings(
        llm=llm,
        max_input_tokens=llm.max_input_tokens,
        output_dir=tmp_path / "out",
        daily_token_budget=1_000_000,
        site_tags={"adhoc": ("security",)},
    )
    post_id, _ = repo.insert_manual_post(conn, repo.ensure_adhoc_site(conn), blog, blog)
    backend = cast(pipeline.Backend, agno_backend)
    lines: list[str] = []
    started = time.monotonic()
    outcome = asyncio.run(
        pipeline.process_post(conn, post_id, settings, backend, fetcher=LocalPages())
    )
    report(lines, "first run", f"{outcome} in {time.monotonic() - started:.1f}s")
    assert outcome.status == "ready" and outcome.tokens > 0
    spent = usage(conn)
    report(lines, "llm_usage", spent)
    total = conn.execute("SELECT SUM(input_tokens + output_tokens) FROM llm_usage").fetchone()
    assert total[0] == outcome.tokens

    files = {a.kind: Path(a.path) for a in repo.artifacts_for(conn, post_id)}
    assert set(files) == {"lesson", "notes", "digest"}
    for path in files.values():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    document = json.loads(files["digest"].read_text(encoding="utf-8"))
    PostDigest.model_validate(document["digest"])
    lesson = LessonSpec.model_validate(document["lesson"])
    post = repo.get_post(conn, post_id)
    assert post is not None and post.status == "ready"
    report(lines, "flags", post.flags)
    report(lines, "final quiz items", len(lesson.final_quiz))
    report(lines, "tokens", document["generation"]["tokens"])
    index = (tmp_path / "out" / "index.html").read_text(encoding="utf-8")
    assert f"adhoc/{files['lesson'].parent.name}/lesson.html" in index

    url = files["lesson"].as_uri()
    for scheme in ("light", "dark"):
        page = open_page(chrome, url, scheme=scheme)
        found = cast(list[str], page.js(A11Y_SCRIPT))
        report(lines, f"a11y {scheme}", found)
        assert found == []
        seen = problems(page)
        report(lines, f"console, csp, network {scheme}", seen)
        assert seen == []

    started = time.monotonic()
    again = asyncio.run(
        pipeline.process_post(conn, post_id, settings, backend, fetcher=LocalPages())
    )
    report(lines, "second run", f"{again} in {time.monotonic() - started:.1f}s")
    assert again.status == "ready" and again.tokens == 0
    assert usage(conn) == spent
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
