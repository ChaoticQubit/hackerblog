import json
import sqlite3
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hackerblog.cli.main import app
from hackerblog.cli.output import clean

runner = CliRunner()


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path),
        "HACKERBLOG_HOME": str(tmp_path / "hb"),
        "HACKERBLOG_CONFIG": "",
        "ANTHROPIC_API_KEY": "",
        "NO_COLOR": "1",
    }


def invoke(env: dict[str, str], *args: str) -> tuple[int, str]:
    result = runner.invoke(app, list(args), env=env)
    return result.exit_code, result.output


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_init_creates_home_config_secrets_and_db(env: dict[str, str], tmp_path: Path) -> None:
    code, out = invoke(env, "init", "--output-dir", str(tmp_path / "out"))
    assert code == 0, out
    home = tmp_path / "hb"
    assert mode(home) == 0o700
    assert mode(home / "secrets.env") == 0o600
    assert (tmp_path / "out").is_dir()
    secrets = (home / "secrets.env").read_text(encoding="utf-8")
    assert "HACKERBLOG_API_TOKEN=" in secrets
    assert "HACKERBLOG_WEBHOOK_SECRET=whsec_" in secrets
    assert "ANTHROPIC_API_KEY=\n" in secrets
    conn = sqlite3.connect(home / "hackerblog.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert "ANTHROPIC_API_KEY" in out
    starter = (home / "sites.toml").read_text(encoding="utf-8")
    assert "backfill_limit" not in starter
    assert "daily_token_budget" not in starter
    assert "[notify]" not in starter


def test_init_twice_keeps_config_and_secrets(env: dict[str, str], tmp_path: Path) -> None:
    invoke(env, "init", "--output-dir", str(tmp_path / "out"))
    secrets = (tmp_path / "hb" / "secrets.env").read_text(encoding="utf-8")
    config = tmp_path / "hb" / "sites.toml"
    config.write_text(config.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    edited = config.read_text(encoding="utf-8")
    code, _ = invoke(env, "init", "--output-dir", str(tmp_path / "out"))
    assert code == 0
    assert config.read_text(encoding="utf-8") == edited
    assert (tmp_path / "hb" / "secrets.env").read_text(encoding="utf-8") == secrets


def test_init_force_without_tty_needs_yes(env: dict[str, str], tmp_path: Path) -> None:
    invoke(env, "init", "--output-dir", str(tmp_path / "out"))
    code, out = invoke(env, "init", "--force")
    assert code == 2
    assert "--yes" in out
    code, _ = invoke(env, "init", "--force", "--yes")
    assert code == 0


def test_init_refuses_unsafe_home(env: dict[str, str], tmp_path: Path) -> None:
    home = tmp_path / "hb"
    home.mkdir(mode=0o755)
    home.chmod(0o755)
    code, out = invoke(env, "init")
    assert code == 3
    assert "chmod 700" in out


def test_check_ok_with_key(env: dict[str, str], tmp_path: Path) -> None:
    invoke(env, "init", "--output-dir", str(tmp_path / "out"))
    code, out = invoke({**env, "ANTHROPIC_API_KEY": "sk-ant-test"}, "check")
    assert code == 0, out
    assert "OK: 0 sites, provider anthropic" in out


def test_check_key_from_secrets_file(env: dict[str, str], tmp_path: Path) -> None:
    invoke(env, "init", "--output-dir", str(tmp_path / "out"))
    secrets = tmp_path / "hb" / "secrets.env"
    text = secrets.read_text(encoding="utf-8")
    secrets.write_text(text.replace("ANTHROPIC_API_KEY=", "ANTHROPIC_API_KEY=sk-ant-x"), "utf-8")
    code, out = invoke(env, "check")
    assert code == 0, out


def test_check_missing_key_exits_10(env: dict[str, str], tmp_path: Path) -> None:
    invoke(env, "init", "--output-dir", str(tmp_path / "out"))
    code, out = invoke(env, "check")
    assert code == 10
    assert "ANTHROPIC_API_KEY" in out


def test_check_bad_config_exits_3(env: dict[str, str], tmp_path: Path) -> None:
    invoke(env, "init", "--output-dir", str(tmp_path / "out"))
    config = tmp_path / "hb" / "sites.toml"
    config.write_text(
        config.read_text(encoding="utf-8") + '\n[[site]]\nurl = "http://10.0.0.1/"\n', "utf-8"
    )
    code, out = invoke(env, "check")
    assert code == 3
    assert "site[0].url" in out
    assert "1 problem" in out


def test_check_unsafe_secrets_exits_3(env: dict[str, str], tmp_path: Path) -> None:
    invoke(env, "init", "--output-dir", str(tmp_path / "out"))
    (tmp_path / "hb" / "secrets.env").chmod(0o644)
    code, out = invoke({**env, "ANTHROPIC_API_KEY": "sk-ant-test"}, "check")
    assert code == 3
    assert "chmod 600" in out


def test_check_json_output(env: dict[str, str], tmp_path: Path) -> None:
    invoke(env, "init", "--output-dir", str(tmp_path / "out"))
    code, out = invoke(env, "check", "--json")
    assert code == 10
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "llm_auth"


def test_check_without_init_exits_3(env: dict[str, str]) -> None:
    code, out = invoke(env, "check")
    assert code == 3
    assert "hackerblog init" in out


def test_config_flag_overrides_env(env: dict[str, str], tmp_path: Path) -> None:
    invoke(env, "init", "--output-dir", str(tmp_path / "out"))
    code, _ = invoke(env, "--config", str(tmp_path / "nope.toml"), "check")
    assert code == 3


def test_version_flag(env: dict[str, str]) -> None:
    code, out = invoke(env, "--version")
    assert code == 0
    assert out.startswith("hackerblog ")


def test_unknown_command_is_usage_error(env: dict[str, str]) -> None:
    code, _ = invoke(env, "frobnicate")
    assert code == 2


def test_clean_strips_terminal_escapes_and_secrets() -> None:
    text = "\x1b[31mred\x1b[0m title\u202e\x07 sk-ant-abcdefghijkl"
    assert clean(text) == "red title [REDACTED]"
