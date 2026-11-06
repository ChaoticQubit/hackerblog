from pathlib import Path

import pytest

from hackerblog.core.config import derive_slug, load_config, validate_cron

VALID = """\
version = 1

[defaults]
schedule = "daily"
backfill_limit = 25
notify = "digest"

[llm]
provider = "anthropic"

[output]
dir = "{out}"

[notify]
digest_time = "08:00"
timezone = "Europe/Berlin"

[[site]]
name = "PortSwigger Research"
url = "https://portswigger.net/research"
tags = ["web-security"]
notify = "instant"

[[site]]
url = "https://www.example.org/writing/"
post_url_pattern = "^https://example\\\\.org/writing/[0-9]{{4}}/[a-z0-9-]+/?$"
schedule = "30 7 * * 1-5"
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "sites.toml"
    path.write_text(text, encoding="utf-8")
    return path


def valid_text(tmp_path: Path) -> str:
    return VALID.format(out=tmp_path / "out")


def test_valid_config_loads_with_defaults(tmp_path: Path) -> None:
    config, problems = load_config(write(tmp_path, valid_text(tmp_path)))
    assert problems == []
    assert config is not None
    assert config.llm.notes_model == "claude-haiku-5-5"
    assert config.llm.lesson_model == "claude-sonnet-5-5"
    assert [s.slug for s in config.sites] == ["portswigger-net-research", "example-org-writing"]
    assert config.sites[0].notify == "instant"
    assert config.sites[1].notify is None
    assert config.fetch.min_delay_seconds == 2.0
    assert config.api.port == 8765
    assert config.defaults.backfill_limit == 25
    assert config.llm.daily_token_budget is None


def test_backfill_and_budget_default_to_no_cap(tmp_path: Path) -> None:
    text = valid_text(tmp_path).replace("backfill_limit = 25\n", "")
    config, problems = load_config(write(tmp_path, text))
    assert problems == []
    assert config is not None
    assert config.defaults.backfill_limit is None
    assert config.sites[0].backfill_limit is None
    assert config.llm.daily_token_budget is None


def test_backfill_and_budget_accept_large_caps(tmp_path: Path) -> None:
    text = valid_text(tmp_path).replace("backfill_limit = 25", "backfill_limit = 50000")
    text = text.replace('provider = "anthropic"', 'provider = "anthropic"\ndaily_token_budget = 0')
    _, problems = load_config(write(tmp_path, text))
    assert [p.location for p in problems] == ["llm.daily_token_budget"]


def test_unknown_key_is_reported_with_line(tmp_path: Path) -> None:
    text = valid_text(tmp_path).replace('notify = "instant"', 'notfy = "instant"')
    config, problems = load_config(write(tmp_path, text))
    assert config is None
    assert len(problems) == 1
    assert problems[0].location == "site[0].notfy"
    assert problems[0].line == text.splitlines().index('notfy = "instant"') + 1


def test_all_problems_reported_at_once(tmp_path: Path) -> None:
    text = valid_text(tmp_path).replace("backfill_limit = 25", "backfill_limit = -1")
    text = text.replace('provider = "anthropic"', 'provider = "anthropic"\nmax_input_tokens = -1')
    _, problems = load_config(write(tmp_path, text))
    locations = sorted(p.location for p in problems)
    assert locations == ["defaults.backfill_limit", "llm.max_input_tokens"]


def test_toml_syntax_error_is_a_problem(tmp_path: Path) -> None:
    config, problems = load_config(write(tmp_path, "version = 1\n[llm\n"))
    assert config is None
    assert problems[0].location == "sites.toml"
    assert problems[0].line == 2


def test_missing_file_is_a_problem(tmp_path: Path) -> None:
    config, problems = load_config(tmp_path / "absent.toml")
    assert config is None
    assert "hackerblog init" in problems[0].hint


def test_duplicate_slug_and_url(tmp_path: Path) -> None:
    text = valid_text(tmp_path) + '\n[[site]]\nurl = "https://portswigger.net/research"\n'
    _, problems = load_config(write(tmp_path, text))
    messages = " ".join(p.message for p in problems)
    assert "duplicate slug" in messages
    assert "duplicate url" in messages


def test_bad_regex_and_cron(tmp_path: Path) -> None:
    text = valid_text(tmp_path).replace('"30 7 * * 1-5"', '"61 7 * * *"')
    text = text.replace("[0-9]{{4}}", "([0-9]").replace("[0-9]{4}", "([0-9]")
    _, problems = load_config(write(tmp_path, text))
    locations = sorted(p.location for p in problems)
    assert locations == ["site[1].post_url_pattern", "site[1].schedule"]


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.org/",
        "http://127.0.0.1/blog",
        "http://10.1.2.3/",
        "http://169.254.169.254/latest",
        "http://[::1]/",
        "http://localhost/",
        "https://user:pw@example.org/",
        "https://example.org:2222/",
    ],
)
def test_url_policy_rejects(tmp_path: Path, url: str) -> None:
    text = valid_text(tmp_path).replace("https://portswigger.net/research", url)
    _, problems = load_config(write(tmp_path, text))
    assert [p.location for p in problems] == ["site[0].url"]


def test_allow_private_hosts_admits_listed_host(tmp_path: Path) -> None:
    text = valid_text(tmp_path).replace("https://portswigger.net/research", "http://127.0.0.1/b")
    text += '\n[fetch]\nallow_private_hosts = ["127.0.0.0/8"]\n'
    _, problems = load_config(write(tmp_path, text))
    assert problems == []


def test_other_providers_need_models(tmp_path: Path) -> None:
    text = valid_text(tmp_path).replace('provider = "anthropic"', 'provider = "ollama"')
    _, problems = load_config(write(tmp_path, text))
    assert sorted(p.location for p in problems) == ["llm.lesson_model", "llm.notes_model"]


def test_ollama_gets_default_base_url(tmp_path: Path) -> None:
    text = valid_text(tmp_path).replace(
        'provider = "anthropic"',
        'provider = "ollama"\nnotes_model = "llama3.2:3b"\nlesson_model = "llama3.2:3b"',
    )
    config, problems = load_config(write(tmp_path, text))
    assert problems == []
    assert config is not None
    assert config.llm.base_url == "http://127.0.0.1:11434/v1"


def test_bad_timezone(tmp_path: Path) -> None:
    text = valid_text(tmp_path).replace("Europe/Berlin", "Mars/Olympus")
    _, problems = load_config(write(tmp_path, text))
    assert [p.location for p in problems] == ["notify.timezone"]


def test_version_must_be_one(tmp_path: Path) -> None:
    text = valid_text(tmp_path).replace("version = 1", "version = 2")
    _, problems = load_config(write(tmp_path, text))
    assert [p.location for p in problems] == ["version"]


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("https://portswigger.net/research", "portswigger-net-research"),
        ("https://www.example.org/writing/", "example-org-writing"),
        ("https://blog.cloudflare.com/", "blog-cloudflare-com"),
    ],
)
def test_derive_slug(url: str, slug: str) -> None:
    assert derive_slug(url) == slug


@pytest.mark.parametrize(
    ("expr", "ok"),
    [
        ("*/15 * * * *", True),
        ("0 7 1-15/2 1,6,12 0-6", True),
        ("0 24 * * *", False),
        ("* * * *", False),
        ("0 7 * jan *", False),
        ("5-1 * * * *", False),
    ],
)
def test_validate_cron(expr: str, ok: bool) -> None:
    assert (validate_cron(expr) is None) == ok
