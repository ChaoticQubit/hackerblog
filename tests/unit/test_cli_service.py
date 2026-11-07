import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hackerblog.cli.main import app

runner = CliRunner()


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    values = {
        "HOME": str(tmp_path),
        "HACKERBLOG_HOME": str(tmp_path / "hb"),
        "HACKERBLOG_CONFIG": "",
        "HACKERBLOG_API_TOKEN": "",
        "NO_COLOR": "1",
    }
    result = runner.invoke(app, ["init", "--output-dir", str(tmp_path / "out")], env=values)
    assert result.exit_code == 0, result.output
    return values


def invoke(env: dict[str, str], *args: str) -> tuple[int, str]:
    result = runner.invoke(app, list(args), env=env)
    return result.exit_code, result.output


def test_serve_without_token_exits_3(env: dict[str, str], tmp_path: Path) -> None:
    secrets = tmp_path / "hb" / "secrets.env"
    lines = secrets.read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines if not line.startswith("HACKERBLOG_API_TOKEN=")]
    secrets.write_text("\n".join(kept) + "\n", encoding="utf-8")
    code, out = invoke(env, "serve")
    assert code == 3
    assert "HACKERBLOG_API_TOKEN" in out


def test_serve_with_bad_config_exits_3(env: dict[str, str], tmp_path: Path) -> None:
    (tmp_path / "hb" / "sites.toml").write_text("version = 2\n", encoding="utf-8")
    code, out = invoke(env, "serve")
    assert code == 3
    assert "hackerblog check" in out


def test_schedule_show_launchd(env: dict[str, str]) -> None:
    code, out = invoke(env, "schedule", "show", "--kind", "launchd")
    assert code == 0, out
    assert "dev.hackerblog.poll.plist" in out
    assert "<key>Hour</key><integer>7</integer><key>Minute</key><integer>30</integer>" in out
    assert "launchctl bootstrap" in out


def test_schedule_show_cron_with_time(env: dict[str, str]) -> None:
    code, out = invoke(env, "schedule", "show", "--kind", "cron", "--time", "06:05")
    assert code == 0, out
    assert "5 6 * * * " in out
    assert " poll >> " in out


def test_schedule_show_systemd_json(env: dict[str, str]) -> None:
    code, out = invoke(env, "schedule", "show", "--kind", "systemd", "--json")
    assert code == 0, out
    data = json.loads(out)["data"]
    assert data["time"] == "07:30"
    names = sorted(Path(name).name for name in data["files"])
    assert names == ["hackerblog-poll.service", "hackerblog-poll.timer"]


@pytest.mark.parametrize("value", ["25:00", "7:30", "07:60", "noon"])
def test_schedule_show_rejects_bad_time(env: dict[str, str], value: str) -> None:
    code, _ = invoke(env, "schedule", "show", "--time", value)
    assert code == 2


def test_schedule_show_rejects_unknown_kind(env: dict[str, str]) -> None:
    code, _ = invoke(env, "schedule", "show", "--kind", "windows")
    assert code == 2


def test_serve_starts_the_scheduler(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    import httpx
    import uvicorn
    from fastapi import FastAPI

    served: list[FastAPI] = []

    def run(app: FastAPI, **_: object) -> None:
        served.append(app)

    async def health(app: FastAPI) -> str:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
                response = await http.get("/v1/health")
        state: str = response.json()["scheduler"]
        return state

    monkeypatch.setattr(uvicorn, "run", run)
    code, out = invoke(env, "serve")
    assert code == 0, out
    assert asyncio.run(health(served[0])) == "running"
