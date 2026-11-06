import socket
import threading
from collections.abc import Iterator, Sequence

import pytest

from hackerblog.core.errors import ExitCode
from hackerblog.core.net.guard import BlockedError, GuardedBackend, Resolver, address_problem


@pytest.mark.parametrize(
    ("ip", "allow", "host", "blocked"),
    [
        ("93.184.216.34", (), "example.com", False),
        ("2606:2800:220:1:248:1893:25c8:1946", (), "example.com", False),
        ("127.0.0.1", (), "example.com", True),
        ("::1", (), "example.com", True),
        ("10.1.2.3", (), "example.com", True),
        ("172.16.0.1", (), "example.com", True),
        ("192.168.1.1", (), "example.com", True),
        ("fd00::1", (), "example.com", True),
        ("169.254.10.10", (), "example.com", True),
        ("fe80::1", (), "example.com", True),
        ("100.64.0.1", (), "example.com", True),
        ("169.254.169.254", (), "example.com", True),
        ("224.0.0.1", (), "example.com", True),
        ("ff02::1", (), "example.com", True),
        ("240.0.0.1", (), "example.com", True),
        ("::", (), "example.com", True),
        ("0.1.2.3", (), "example.com", True),
        ("::ffff:127.0.0.1", (), "example.com", True),
        ("::ffff:169.254.169.254", (), "example.com", True),
        ("::ffff:93.184.216.34", (), "example.com", False),
        ("64:ff9b::a9fe:a9fe", (), "example.com", True),
        ("64:ff9b::7f00:1", (), "example.com", True),
        ("64:ff9b::808:808", (), "example.com", False),
        ("::127.0.0.1", (), "example.com", True),
        ("::a00:1", (), "example.com", True),
        ("::808:808", (), "example.com", False),
        ("fec0::1", (), "example.com", True),
        ("not-an-ip", (), "example.com", True),
        ("10.1.2.3", ("intranet.example",), "intranet.example", False),
        ("10.1.2.3", ("Intranet.Example",), "intranet.example", False),
        ("10.1.2.3", ("intranet.example",), "other.example", True),
        ("10.1.2.3", ("10.0.0.0/8",), "other.example", False),
        ("::ffff:10.1.2.3", ("10.0.0.0/8",), "other.example", False),
        ("192.168.1.1", ("10.0.0.0/8",), "other.example", True),
        ("::1", ("127.0.0.0/8",), "other.example", True),
    ],
)
def test_address_problem_table(ip: str, allow: Sequence[str], host: str, blocked: bool) -> None:
    problem = address_problem(ip, allow, host)
    assert (problem is not None) == blocked
    assert problem is None or problem != ""


def fixed_resolver(*answers: Sequence[str]) -> Resolver:
    """Return an async resolver that gives answers[0] first, then answers[1], and so on."""
    remaining = list(answers)

    async def resolve(host: str, port: int) -> Sequence[str]:
        assert host
        assert port > 0
        return remaining.pop(0)

    return resolve


@pytest.fixture
def listener() -> Iterator[int]:
    """Accept TCP connections on 127.0.0.1 in a thread and yield the port."""
    server = socket.create_server(("127.0.0.1", 0))
    server.settimeout(5)
    accepted: list[socket.socket] = []

    def accept() -> None:
        try:
            while True:
                accepted.append(server.accept()[0])
        except OSError:
            return

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield server.getsockname()[1]
    server.close()
    for connection in accepted:
        connection.close()


@pytest.mark.anyio
async def test_private_answer_is_refused() -> None:
    backend = GuardedBackend((), resolver=fixed_resolver(["10.0.0.7"]))
    with pytest.raises(BlockedError) as caught:
        await backend.connect_tcp("blog.example", 80)
    assert caught.value.reason == "ssrf"
    assert caught.value.code == ExitCode.BLOCKED


@pytest.mark.anyio
async def test_mixed_public_and_private_answer_is_refused() -> None:
    backend = GuardedBackend((), resolver=fixed_resolver(["93.184.216.34", "127.0.0.1"]))
    with pytest.raises(BlockedError) as caught:
        await backend.connect_tcp("blog.example", 80)
    assert caught.value.reason == "ssrf"


@pytest.mark.anyio
async def test_empty_answer_is_refused() -> None:
    backend = GuardedBackend((), resolver=fixed_resolver([]))
    with pytest.raises(BlockedError):
        await backend.connect_tcp("blog.example", 80)


@pytest.mark.anyio
async def test_dns_rebinding_second_connection_refused(listener: int) -> None:
    resolver = fixed_resolver(["127.0.0.1"], ["10.0.0.7"])
    backend = GuardedBackend(("127.0.0.1/32",), resolver=resolver)
    stream = await backend.connect_tcp("rebind.example", listener)
    assert stream.get_extra_info("server_addr")[0] == "127.0.0.1"
    await stream.aclose()
    with pytest.raises(BlockedError) as caught:
        await backend.connect_tcp("rebind.example", listener)
    assert caught.value.reason == "ssrf"


@pytest.mark.anyio
async def test_default_resolver_vets_localhost() -> None:
    backend = GuardedBackend(())
    with pytest.raises(BlockedError):
        await backend.connect_tcp("localhost", 80)
