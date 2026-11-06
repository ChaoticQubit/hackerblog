import tomllib

from hackerblog.core.discovery.service import toml_block


def test_toml_block_round_trips_hostile_titles() -> None:
    name = 'Emoji 🚀 "quoted" \\ back\nline\ttab\x7fdel\x00nul \u2028sep'
    text = toml_block({"url": "https://a.example/", "name": name, "tags": ["x", "y"], "n": 0})
    parsed = tomllib.loads(text)
    assert parsed["site"] == [
        {"url": "https://a.example/", "name": name, "tags": ["x", "y"], "n": 0}
    ]


def test_toml_block_skips_unset_values() -> None:
    text = toml_block({"url": "https://a.example/", "name": None, "tags": []})
    assert text == '[[site]]\nurl = "https://a.example/"\n'
