"""Offline URL policy: the checks that need no DNS lookup (section 8.1)."""

import ipaddress
import re
from collections.abc import Sequence
from urllib.parse import urlsplit, urlunsplit

MAX_URL_LENGTH = 2048
ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})
NAT64_NETWORK = ipaddress.ip_network("64:ff9b::/96")
COMPAT_NETWORK = ipaddress.ip_network("::/96")
SITE_LOCAL_NETWORK = ipaddress.ip_network("fec0::/10")
LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
DEFAULT_PORTS = {"http": 80, "https": 443}
TRACKING_KEYS = frozenset({"fbclid", "gclid", "ref"})
SLASHES = re.compile(r"/{2,}")


def is_allowlisted(host: str, ip: IPAddress | None, allow: Sequence[str]) -> bool:
    """Return whether host, or its literal ip, matches an allow_private_hosts entry."""
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


def embedded_ipv4(address: ipaddress.IPv6Address) -> IPAddress:
    """Return the IPv4 inside a mapped, NAT64 or IPv4-compatible address, else address."""
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    in_compat = address in COMPAT_NETWORK and int(address) > 1
    if address in NAT64_NETWORK or in_compat:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return address


def _literal_ip(host: str) -> IPAddress | None:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address):
        return embedded_ipv4(ip)
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
    if is_allowlisted(host, ip, allow_private_hosts):
        return None
    if port is not None and port not in ALLOWED_PORTS:
        return f"port {port} is not allowed (80, 443, 8080, 8443)"
    if ip is not None and (not ip.is_global or ip in SITE_LOCAL_NETWORK):
        return f"{host} is not a public address"
    if host == "localhost" or host.endswith(LOCAL_SUFFIXES):
        return f"{host} is a local host name"
    return None


def _is_tracking(pair: str) -> bool:
    key = pair.split("=", 1)[0].lower()
    return key.startswith("utm_") or key in TRACKING_KEYS


def canonicalize(url: str) -> str:
    """Return the canonical form of an absolute http(s) url (section 8.4).

    Lowercases scheme and host, drops the default port, fragment and tracking query keys,
    sorts the remaining query pairs by key, collapses duplicate slashes and keeps a trailing
    slash as found; an empty path becomes "/". Raises ValueError for other URLs.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS:
        raise ValueError(f"not an http(s) URL: {url!r}")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError(f"URL has no host: {url!r}")
    port = parts.port
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"
    path = SLASHES.sub("/", parts.path) or "/"
    pairs = [pair for pair in parts.query.split("&") if pair and not _is_tracking(pair)]
    query = "&".join(sorted(pairs, key=lambda pair: pair.split("=", 1)[0]))
    canonical = urlunsplit((scheme, netloc, path, query, ""))
    assert canonical.startswith(f"{scheme}://{netloc}/"), canonical
    assert "#" not in canonical, canonical
    return canonical
