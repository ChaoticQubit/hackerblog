import sys
from pathlib import Path

import pytest

from hackerblog.cli import main
from hackerblog.core.errors import ExitCode, HackerBlogError
from hackerblog.core.service import CheckReport

BROKEN = "version = 2\n"


def run_cli(monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["hackerblog", *args])
    with pytest.raises(SystemExit) as stopped:
        main.run()
    code = stopped.value.code
    return 0 if code is None else int(code)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "hb"
    for name, value in {
        "HOME": str(tmp_path),
        "HACKERBLOG_HOME": str(home),
        "HACKERBLOG_CONFIG": "",
        "ANTHROPIC_API_KEY": "",
        "NO_COLOR": "1",
    }.items():
        monkeypatch.setenv(name, value)
    assert run_cli(monkeypatch, "init", "--output-dir", str(tmp_path / "out")) == 0
    return home


@pytest.mark.parametrize(
    "case",
    [
        (("init", "--force"), None, ExitCode.USAGE),
        (("check",), None, ExitCode.LLM_AUTH),
        (("check", "--json"), BROKEN, ExitCode.CONFIG),
        (("sites", "show", "nope"), None, ExitCode.NOT_FOUND),
        (("posts", "show", "999"), None, ExitCode.NOT_FOUND),
        (("posts", "list", "--site", "nope"), None, ExitCode.NOT_FOUND),
        (("poll", "--site", "nope"), None, ExitCode.NOT_FOUND),
        (("schedule", "show"), BROKEN, ExitCode.CONFIG),
        (("serve",), BROKEN, ExitCode.CONFIG),
        (("work",), BROKEN, ExitCode.CONFIG),
        (("render", "--site", "nope"), None, ExitCode.NOT_FOUND),
        (("regenerate", "999"), None, ExitCode.LLM_AUTH),
        (("open", "999", "--print"), None, ExitCode.NOT_FOUND),
        (("quiz", "999"), None, ExitCode.NOT_FOUND),
        (("search", "words", "--site", "nope"), None, ExitCode.NOT_FOUND),
        (("ask", "-i", "--json"), None, ExitCode.USAGE),
    ],
)
def test_a_failing_command_prints_error_and_exits_with_its_code(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: tuple[tuple[str, ...], str | None, ExitCode],
) -> None:
    args, config, expected = case
    if config is not None:
        (home / "sites.toml").write_text(config, encoding="utf-8")
    capsys.readouterr()
    code = run_cli(monkeypatch, *args)
    captured = capsys.readouterr()
    assert code == expected, (args, captured)
    assert "Error:" in captured.err or '"ok": false' in captured.out, (args, captured)


def test_an_uncaught_hackerblog_error_exits_with_its_own_code(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*_: object) -> CheckReport:
        raise HackerBlogError(ExitCode.LOCKED, "the database is locked", "hackerblog work")

    monkeypatch.setattr(main.service, "check", refuse)
    capsys.readouterr()
    assert run_cli(monkeypatch, "check") == ExitCode.LOCKED
    err = capsys.readouterr().err
    assert "Error: the database is locked" in err
    assert "Try: hackerblog work" in err
    assert "Internal error" not in err
    assert run_cli(monkeypatch, "check", "--json") == ExitCode.LOCKED
    assert '"code": "locked"' in capsys.readouterr().out
