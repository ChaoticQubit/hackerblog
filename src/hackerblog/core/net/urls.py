"""Offline URL policy: the checks that need no DNS lookup (section 8.1)."""

import ipaddress
from collections.abc import Sequence
from urllib.parse import urlsplit

MAX_URL_LENGTH = 2048
ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})
LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def _is_allowlisted(host: str, ip: IPAddress | None, allow: Sequence[str]) -> bool:
    for entry in allow:
        if entry.lower() == host:
            return True
        if ip is None:
            continue
        try:
            network: IPNetwork = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue
        if ip.version == network.version and ip in network:
            return True
    return False


def _literal_ip(host: str) -> IPAddress | None:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def url_policy_problem(url: str, allow_private_hosts: Sequence[str] = ()) -> str | None:
    """Return why url breaks the URL policy, or None when it passes.

    Rejects non-http(s) schemes, userinfo, over-long URLs, unusual ports and hosts that are
    literal non-global IPs or local names, unless the host is in allow_private_hosts
    (host names or CIDRs). Hostnames are not resolved here; the fetcher checks resolved IPs.
    """
    if len(url) > MAX_URL_LENGTH:
        return f"URL is longer than {MAX_URL_LENGTH} characters"
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as error:
        return f"URL does not parse: {error}"
    if parts.scheme not in {"http", "https"}:
        return "URL scheme must be http or https"
    host = (parts.hostname or "").lower()
    if not host:
        return "URL has no host"
    if parts.username is not None or parts.password is not None:
        return "URL must not contain a user name or password"
    ip = _literal_ip(host)
    if _is_allowlisted(host, ip, allow_private_hosts):
        return None
    if port is not None and port not in ALLOWED_PORTS:
        return f"port {port} is not allowed (80, 443, 8080, 8443)"
    if ip is not None and not ip.is_global:
        return f"{host} is not a public address"
    if host == "localhost" or host.endswith(LOCAL_SUFFIXES):
        return f"{host} is a local host name"
    return None
