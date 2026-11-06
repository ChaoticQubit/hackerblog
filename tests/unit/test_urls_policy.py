import pytest

from hackerblog.core.net.urls import url_policy_problem


@pytest.mark.parametrize(
    "url",
    [
        "http://[64:ff9b::a9fe:a9fe]/",
        "http://[64:ff9b::7f00:1]/",
        "http://[::127.0.0.1]/",
        "http://[::a00:1]/",
        "http://[::ffff:10.0.0.1]/",
        "http://[fec0::1]/",
        "http://169.254.169.254/",
    ],
)
def test_literal_ips_hiding_private_ipv4_are_refused(url: str) -> None:
    problem = url_policy_problem(url)
    assert problem is not None
    assert "not a public address" in problem


@pytest.mark.parametrize("url", ["http://[64:ff9b::808:808]/", "https://8.8.8.8/"])
def test_literal_ips_embedding_public_ipv4_pass(url: str) -> None:
    assert url_policy_problem(url) is None


def test_allowlisted_cidr_matches_the_unwrapped_ipv4() -> None:
    assert url_policy_problem("http://[64:ff9b::7f00:1]:8000/", ["127.0.0.0/8"]) is None
