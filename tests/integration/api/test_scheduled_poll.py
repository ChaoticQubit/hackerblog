import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from tests.integration.api.test_service import TOKEN, serving
from tests.integration.discovery.server import serve_blogs

from hackerblog.api.app import ServiceSettings, create_app
from hackerblog.api.scheduler import discovery_poll
from hackerblog.core.discovery.service import load_valid_config
from hackerblog.core.paths import resolve_paths
from hackerblog.core.store.migrate import connect, migrate


def stored_posts(db: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT s.slug, r.trigger FROM posts p JOIN sites s ON s.id = p.site_id"
            " JOIN poll_runs r ON r.id = (SELECT MAX(id) FROM poll_runs)"
        ).fetchall()
    finally:
        conn.close()


def test_scheduled_job_polls_its_site_and_stores_posts(tmp_path: Path) -> None:
    paths = resolve_paths(home=tmp_path / "hb")
    paths.home.mkdir(mode=0o700)
    with serve_blogs() as blogs:
        paths.config.write_text(
            "\n".join(
                [
                    "version = 1",
                    "[output]",
                    f"dir = {json.dumps(str(tmp_path / 'out'))}",
                    "[notify]",
                    'timezone = "UTC"',
                    "[fetch]",
                    "min_delay_seconds = 0.5",
                    'allow_private_hosts = ["127.0.0.1"]',
                    "[[site]]",
                    f'url = "{blogs.base_url}/feedblog/"',
                    'slug = "feedblog"',
                ]
            ),
            encoding="utf-8",
        )
        conn = connect(paths.db)
        migrate(conn)
        conn.close()
        settings = ServiceSettings(token=TOKEN, paths=paths, config=load_valid_config(paths))
        app = create_app(settings, poll=discovery_poll(paths))
        with serving(app) as (base, _):
            assert httpx.get(f"{base}/v1/health").json()["scheduler"] == "running"
            assert stored_posts(paths.db) == []
            app.state.scheduler.modify_job("poll:feedblog", next_run_time=datetime.now(UTC))
            deadline = time.monotonic() + 30
            while not stored_posts(paths.db):
                assert time.monotonic() < deadline, "the scheduled poll stored no posts"
                time.sleep(0.2)
    posts = stored_posts(paths.db)
    assert {slug for slug, _ in posts} == {"feedblog"}
    assert {trigger for _, trigger in posts} == {"scheduler"}
    assert len(posts) >= 3
