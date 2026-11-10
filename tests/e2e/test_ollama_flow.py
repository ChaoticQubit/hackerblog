"""End-to-end flows against a real local Ollama model, with no mocks.

A fixture blog runs on 127.0.0.1 and the real CLI runs in a throwaway HOME. Every call to
Ollama goes through a recording proxy, so a poll that should cost nothing is proven to make
no model call. Each test writes its command log under .local-state/e2e/.
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TextIO, cast
from urllib.parse import urlsplit

import httpx
import pytest
from scripts.scan_service import free_port, wait_healthy
from tests.integration.discovery.server import serve_blogs
from tests.integration.processing.live import OLLAMA_MODEL, OLLAMA_URL, ollama_has_model

from hackerblog.core.render.sanitize import csp_hash

pytestmark = [
    pytest.mark.integration,
    pytest.mark.live,
    pytest.mark.skipif(not ollama_has_model(), reason=f"Ollama at {OLLAMA_URL} lacks a model"),
]

CLI = [sys.executable, "-c", "from hackerblog.cli.main import run; run()"]
LOGS = Path(__file__).resolve().parents[2] / ".local-state" / "e2e"
REQUEST_TIMEOUT_S = 900
PROCESS_DEADLINE_S = 2400
COMMAND_TIMEOUT_S = 2700
WAIT_STEP_S = 10
SCRIPT = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.DOTALL)
STYLE = re.compile(r"<style[^>]*>(?P<body>.*?)</style>", re.DOTALL)


class _Proxy(ThreadingHTTPServer):
    daemon_threads = True
    upstream: tuple[str, int]
    paths: list[str]
    lock: threading.Lock


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def _forward(self) -> None:
        proxy = cast(_Proxy, self.server)
        with proxy.lock:
            proxy.paths.append(f"{self.command} {self.path}")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {"Content-Type": self.headers.get("Content-Type") or "application/json"}
        upstream = HTTPConnection(*proxy.upstream, timeout=REQUEST_TIMEOUT_S)
        try:
            upstream.request(self.command, self.path, body=body, headers=headers)
            response = upstream.getresponse()
            payload = response.read()
        finally:
            upstream.close()
        self.send_response(response.status)
        self.send_header("Content-Type", response.getheader("Content-Type") or "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@contextmanager
def recording_proxy() -> Generator[_Proxy]:
    """Serve a proxy to Ollama on 127.0.0.1 that records "METHOD path" per request."""
    parts = urlsplit(OLLAMA_URL)
    proxy = _Proxy(("127.0.0.1", 0), _ProxyHandler)
    proxy.upstream = (parts.hostname or "127.0.0.1", parts.port or 80)
    proxy.paths = []
    proxy.lock = threading.Lock()
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    try:
        yield proxy
    finally:
        proxy.shutdown()
        proxy.server_close()
        thread.join(timeout=10)


class Run:
    """The CLI in one throwaway home, logging every command and its output to log."""

    def __init__(self, home: Path, log: TextIO) -> None:
        self.home = home
        self.log = log
        self.env = {
            **os.environ,
            "HOME": str(home),
            "HACKERBLOG_HOME": str(home / "hb"),
            "HACKERBLOG_CONFIG": "",
            "HACKERBLOG_API_TOKEN": "",
        }

    def note(self, text: str) -> None:
        """Write one timestamped line to the log."""
        self.log.write(f"[{datetime.now(UTC).isoformat(timespec='seconds')}] {text}\n")
        self.log.flush()

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run hackerblog with args, log it and return the finished process."""
        self.note("$ hackerblog " + " ".join(args))
        started = time.monotonic()
        result = subprocess.run(
            [*CLI, *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_S,
            check=False,
        )
        self.note(f"exit {result.returncode} after {time.monotonic() - started:.1f} s")
        self.log.write(result.stdout + result.stderr + "\n")
        self.log.flush()
        return result

    def data(self, *args: str) -> Any:
        """Run hackerblog with args and --json, require success and return its data."""
        result = self.cli(*args, "--json")
        assert result.returncode == 0, result.stdout + result.stderr
        document = json.loads(result.stdout)
        assert document["ok"] is True, document
        return document["data"]

    def setup(self, blog: str, base_url: str, schedule: str | None = None) -> str:
        """Init for Ollama, finish the config, follow one fixture blog and return its slug."""
        out = self.home / "out"
        assert self.cli("init", "--provider", "ollama", "--output-dir", str(out)).returncode == 0
        config = self.home / "hb" / "sites.toml"
        text = config.read_text(encoding="utf-8")
        assert "notes_model" not in text and "lesson_model" not in text, text
        self.note("finding: init --provider ollama leaves notes_model and lesson_model unset")
        llm = (
            f'notes_model = "{OLLAMA_MODEL}"\nlesson_model = "{OLLAMA_MODEL}"\n'
            f'base_url = "{base_url}/v1"\nrequest_timeout_seconds = {REQUEST_TIMEOUT_S}\n'
        )
        text = text.replace('provider = "ollama"\n', f'provider = "ollama"\n{llm}', 1)
        text += '\n[fetch]\nallow_private_hosts = ["127.0.0.1"]\n'
        if schedule is not None:
            text = text.replace('schedule = "daily"', f'schedule = "{schedule}"', 1)
        config.write_text(text, encoding="utf-8")
        assert self.cli("check").returncode == 0
        site = self.data("sites", "add", blog, "--backfill-limit", "1")
        slug: str = site["slug"]
        self.note(f"config:\n{config.read_text(encoding='utf-8')}")
        return slug

    def ready_posts(self) -> list[dict[str, Any]]:
        """Return the posts `posts list` shows as ready."""
        posts: list[dict[str, Any]] = self.data("posts", "list", "--status", "ready")
        assert all(post["status"] == "ready" for post in posts), posts
        return posts

    def lesson_dirs(self) -> list[Path]:
        """Return every output folder holding a non-empty lesson.html and notes.md."""
        found = sorted(path.parent for path in (self.home / "out").glob("*/*/lesson.html"))
        for folder in found:
            assert (folder / "lesson.html").stat().st_size > 0, folder
            assert (folder / "notes.md").stat().st_size > 0, folder
        return found


@pytest.fixture
def run(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Run]:
    """A Run in tmp_path that logs to .local-state/e2e/<test name>.log."""
    LOGS.mkdir(parents=True, exist_ok=True)
    with (LOGS / f"{request.node.name}.log").open("w", encoding="utf-8") as log:
        yield Run(tmp_path, log)


def llm_tokens(home: Path) -> int:
    """Return the input plus output tokens the store recorded across all model calls."""
    with sqlite3.connect(home / "hb" / "hackerblog.db") as db:
        row = db.execute("SELECT COALESCE(SUM(input_tokens + output_tokens), 0) FROM llm_usage")
        total: int = row.fetchone()[0]
    return total


def model_calls(proxy: _Proxy) -> list[str]:
    """Return the recorded requests that ask the model for a completion."""
    with proxy.lock:
        return [path for path in proxy.paths if path.startswith("POST ")]


def directives(policy: str) -> dict[str, list[str]]:
    """Split a CSP header into directive name mapped to its source list."""
    parsed: dict[str, list[str]] = {}
    for part in policy.split(";"):
        words = part.split()
        if words:
            parsed[words[0]] = words[1:]
    return parsed


def assert_quiz_script_runs(page: str, policy: str, scratch: Path) -> list[str]:
    """Check every inline script and style is allowed by the served CSP; return the hashes.

    The quiz script must also parse as JavaScript when node is installed.
    """
    allowed = directives(policy)
    assert "'unsafe-inline'" not in policy, policy
    scripts = [
        match["body"] for match in SCRIPT.finditer(page) if "application/json" not in match["attrs"]
    ]
    styles = [match["body"] for match in STYLE.finditer(page)]
    assert scripts, "lesson has no quiz script"
    assert styles, "lesson has no style"
    for body in scripts:
        assert csp_hash(body) in allowed.get("script-src", []), (csp_hash(body), policy)
    for body in styles:
        assert csp_hash(body) in allowed.get("style-src", []), (csp_hash(body), policy)
    node = shutil.which("node")
    if node is not None:
        for index, body in enumerate(scripts):
            source = scratch / f"lesson-{index}.js"
            source.write_text(body, encoding="utf-8")
            check = subprocess.run([node, "--check", str(source)], capture_output=True, check=False)
            assert check.returncode == 0, check.stderr
    return [csp_hash(body) for body in scripts]


def test_poll_processes_new_posts_and_a_second_poll_costs_nothing(run: Run) -> None:
    with serve_blogs() as blogs, recording_proxy() as proxy:
        proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"
        slug = run.setup(f"{blogs.base_url}/feedblog/", proxy_url)

        first = run.data("poll")
        run.note(f"first poll tokens={first['tokens']} model calls={len(model_calls(proxy))}")
        assert first["new_posts"] >= 1, first
        assert first["jobs"], first
        assert all(job["state"] == "ready" for job in first["jobs"]), first["jobs"]
        assert first["tokens"] > 0, first
        assert model_calls(proxy), proxy.paths

        posts = run.ready_posts()
        folders = run.lesson_dirs()
        assert {post["id"] for post in posts} == {job["post_id"] for job in first["jobs"]}
        assert len(folders) == len(posts), folders
        assert all(folder.parent.name == slug for folder in folders), folders
        run.note("lessons: " + ", ".join(str(folder / "lesson.html") for folder in folders))

        calls_before = len(model_calls(proxy))
        tokens_before = llm_tokens(run.home)
        second = run.data("poll")
        run.note(f"second poll tokens={second['tokens']} model calls={len(model_calls(proxy))}")
        assert second["tokens"] == 0, second
        assert second["new_posts"] == 0, second
        assert second["jobs"] == [], second
        assert len(model_calls(proxy)) == calls_before, proxy.paths
        assert llm_tokens(run.home) == tokens_before


def test_serve_scheduler_and_worker_produce_a_served_lesson(run: Run) -> None:
    with serve_blogs() as blogs, recording_proxy() as proxy:
        proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"
        run.setup(f"{blogs.base_url}/feedblog/", proxy_url, schedule="* * * * *")
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        run.note(f"$ hackerblog serve --port {port}")
        with (LOGS / "serve.log").open("wb") as serve_log:
            server = subprocess.Popen(
                [*CLI, "serve", "--port", str(port)],
                env=run.env,
                stdout=serve_log,
                stderr=subprocess.STDOUT,
            )
            try:
                wait_healthy(f"{base}/v1/health", server)
                health = httpx.get(f"{base}/v1/health", timeout=5).json()
                run.note(f"health: {health}")
                assert health["scheduler"] == "running", health
                assert health["worker"] == "running", health

                deadline = time.monotonic() + PROCESS_DEADLINE_S
                posts = run.ready_posts()
                while not posts:
                    assert server.poll() is None, "serve exited"
                    assert time.monotonic() < deadline, "no ready lesson in time"
                    time.sleep(WAIT_STEP_S)
                    posts = run.ready_posts()

                with sqlite3.connect(run.home / "hb" / "hackerblog.db") as db:
                    triggers = [row[0] for row in db.execute("SELECT trigger FROM poll_runs")]
                run.note(f"poll_runs triggers: {triggers}")
                assert "scheduler" in triggers, triggers
                assert "cli" not in triggers, triggers
                assert model_calls(proxy), proxy.paths

                folders = run.lesson_dirs()
                assert folders, "no lesson on disk"
                out = run.home / "out"
                for folder in folders:
                    path = (folder / "lesson.html").relative_to(out).as_posix()
                    response = httpx.get(f"{base}/files/{path}", timeout=10)
                    policy = response.headers.get("content-security-policy", "")
                    run.note(f"GET /files/{path} -> {response.status_code}")
                    run.note(f"headers: {dict(response.headers)}")
                    assert response.status_code == 200
                    assert response.headers["content-type"].startswith("text/html")
                    assert response.text == (folder / "lesson.html").read_text(encoding="utf-8")
                    hashes = assert_quiz_script_runs(response.text, policy, run.home)
                    run.note(f"quiz script hashes allowed by served CSP: {hashes}")
            finally:
                server.terminate()
                server.wait(timeout=30)
