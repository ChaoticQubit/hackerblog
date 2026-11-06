"""Connect-time SSRF guard: vet every resolved address before a socket opens (section 8.1)."""

import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any, Literal, cast

import anyio
import httpcore

from hackerblog.core.errors import ExitCode, HackerBlogError
from hackerblog.core.net.urls import SITE_LOCAL_NETWORK, IPAddress, embedded_ipv4, is_allowlisted

Resolver = Callable[[str, int], Awaitable[Sequence[str]]]
BlockReason = Literal["ssrf", "robots", "policy"]
ADDRESSES_MAX = 32
METADATA_IP = ipaddress.ip_address("169.254.169.254")
ZERO_NETWORK = ipaddress.ip_network("0.0.0.0/8")
CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


class BlockedError(HackerBlogError):
    """A request refused by the SSRF guard, robots.txt or the URL policy."""

    def __init__(self, reason: BlockReason, message: str, hint: str = "") -> None:
        assert reason in {"ssrf", "robots", "policy"}
        assert message
        super().__init__(ExitCode.BLOCKED, message, hint)
        self.reason: BlockReason = reason


def address_problem(ip: str, allow_private_hosts: Sequence[str], host: str) -> str | None:
    """Return why connecting to ip for host is refused, or None when it may connect.

    IPv4-mapped, NAT64 (64:ff9b::/96) and IPv4-compatible (::/96 except :: and ::1) IPv6
    are unwrapped to their embedded IPv4 first. Only globally routable unicast passes, unless host
    or a CIDR containing ip is in allow_private_hosts.
    """
    assert host
    try:
        parsed: IPAddress = ipaddress.ip_address(ip)
    except ValueError:
        return f"{ip!r} is not an IP address"
    if isinstance(parsed, ipaddress.IPv6Address):
        parsed = embedded_ipv4(parsed)
    if is_allowlisted(host.lower(), parsed, allow_private_hosts):
        return None
    if parsed in SITE_LOCAL_NETWORK:
        return f"{host} resolves to the site-local address {parsed}"
    if parsed == METADATA_IP:
        return f"{host} resolves to the cloud metadata address {parsed}"
    if parsed in ZERO_NETWORK or parsed in CGNAT_NETWORK:
        return f"{host} resolves to the non-routable address {parsed}"
    if parsed.is_multicast or parsed.is_reserved or parsed.is_unspecified:
        return f"{host} resolves to the multicast or reserved address {parsed}"
    if parsed.is_loopback or parsed.is_link_local or parsed.is_private or not parsed.is_global:
        return f"{host} resolves to the non-public address {parsed}"
    return None


async def resolve_host(host: str, port: int) -> Sequence[str]:
    """Resolve host to its unique addresses in resolver order, at most ADDRESSES_MAX."""
    assert host
    assert 0 < port < 65536
    try:
        infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise httpcore.ConnectError(f"cannot resolve {host}: {error}") from error
    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in addresses:
            addresses.append(address)
    return addresses[:ADDRESSES_MAX]


class GuardedBackend(httpcore.AsyncNetworkBackend):
    """An httpcore backend that resolves, vets every address, then connects to a vetted IP.

    The caller (httpcore) still runs TLS with the original host name as SNI and for
    certificate checks, because only the TCP target changes.
    """

    def __init__(
        self,
        allow_private_hosts: Sequence[str],
        *,
        resolver: Resolver | None = None,
        inner: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._allow = tuple(allow_private_hosts)
        self._resolver: Resolver = resolver or resolve_host
        self._inner = inner or cast(httpcore.AsyncNetworkBackend, httpcore.AnyIOBackend())

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Open a TCP stream to host:port only if every resolved address passes the guard."""
        assert host
        assert 0 < port < 65536
        addresses = list(await self._resolver(host, port))
        if not addresses:
            raise BlockedError("ssrf", f"{host} resolved to no address")
        assert len(addresses) <= ADDRESSES_MAX
        for address in addresses:
            problem = address_problem(address, self._allow, host)
            if problem is not None:
                raise BlockedError("ssrf", problem, "add the host to fetch.allow_private_hosts")
        return await self._inner.connect_tcp(
            addresses[0],
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Refuse Unix sockets: the fetcher only talks TCP to vetted addresses."""
        assert path
        raise BlockedError("ssrf", "Unix socket connections are not allowed")

    async def sleep(self, seconds: float) -> None:
        """Sleep through the inner backend, as httpcore's retry logic expects."""
        assert seconds >= 0
        await self._inner.sleep(seconds)
