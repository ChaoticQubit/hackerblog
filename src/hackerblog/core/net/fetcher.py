"""The one outbound HTTP client: guarded, polite, size-capped and conditional (section 8.1)."""

import sqlite3
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Literal, Protocol, Self
from urllib.parse import urljoin, urlsplit

import anyio
import httpcore
import httpx

import hackerblog
from hackerblog.core.config import FetchConfig
from hackerblog.core.errors import ExitCode, HackerBlogError
from hackerblog.core.net.guard import BlockedError, GuardedBackend, Resolver
from hackerblog.core.net.ratelimit import HostLimiter
from hackerblog.core.net.robots import RobotsPolicy, load_policy, origin_of, store_policy
from hackerblog.core.net.urls import url_policy_problem

Kind = Literal["page", "feed", "sitemap"]
MIB = 1024 * 1024
BODY_BYTES_MAX: dict[Kind, int] = {"page": 5 * MIB, "feed": 5 * MIB, "sitemap": 20 * MIB}
ROBOTS_BYTES_MAX = 512 * 1024
REDIRECTS_MAX = 5
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
RETRY_STATUSES = frozenset({429, 503})
RETRY_AFTER_S_MAX = 24 * 60 * 60.0
RETRY_DEFAULT_S = 60 * 60.0
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
TOTAL_S = 60.0
GZIP_MAGIC = b"\x1f\x8b"
GZIP_WBITS = 16 + zlib.MAX_WBITS
PROJECT_URL = "https://github.com/hackerblog/hackerblog"


@dataclass(frozen=True)
class Response:
    """A finished GET: final URL, status, decoded body (empty on 304), lowercase headers."""

    url: str
    status: int
    body: bytes
    headers: dict[str, str]

    @property
    def not_modified(self) -> bool:
        """True when the server answered 304 to a conditional GET."""
        return self.status == 304


class FetchError(HackerBlogError):
    """A GET that failed: network error, timeout, size cap, redirect cap, 429 or 503."""

    def __init__(
        self, message: str, *, status: int | None = None, retry_after_s: float | None = None
    ) -> None:
        assert retry_after_s is None or 0 <= retry_after_s <= RETRY_AFTER_S_MAX
        assert status is None or 100 <= status <= 599
        super().__init__(ExitCode.FAILED, message)
        self.status = status
        self.retry_after_s = retry_after_s


class _Inflater(Protocol):
    """The part of a zlib decompress object the bounded reader uses."""

    @property
    def unconsumed_tail(self) -> bytes: ...

    def decompress(self, data: bytes, max_length: int = 0, /) -> bytes: ...

    def flush(self, length: int = ..., /) -> bytes: ...


@dataclass(frozen=True)
class _Exchange:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    location: str | None


class _GuardedTransport(httpx.AsyncHTTPTransport):
    """httpx's transport with its connection pool rebuilt on the SSRF-guarded backend."""

    def __init__(self, backend: GuardedBackend, hosts_max: int) -> None:
        assert hosts_max >= 1
        ssl_context = httpx.create_ssl_context(verify=True, trust_env=False)
        super().__init__(verify=ssl_context, trust_env=False, retries=0)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=hosts_max * 2,
            max_keepalive_connections=hosts_max,
            keepalive_expiry=5.0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=backend,
        )


def _inflate(decoder: _Inflater, data: bytes, budget: int) -> bytes:
    assert budget >= 0
    try:
        piece = decoder.decompress(data, budget + 1)
    except zlib.error as error:
        raise FetchError(f"compressed body is corrupt: {error}") from error
    if len(piece) > budget:
        raise FetchError("decoded body is larger than the size cap")
    assert not decoder.unconsumed_tail
    return piece


def _decoder(encoding: str) -> _Inflater | None:
    assert encoding == encoding.lower()
    if encoding in {"", "identity"}:
        return None
    if encoding in {"gzip", "x-gzip"}:
        return zlib.decompressobj(wbits=GZIP_WBITS)
    if encoding == "deflate":
        return zlib.decompressobj(wbits=zlib.MAX_WBITS)
    raise FetchError(f"unsupported Content-Encoding {encoding!r}")


async def _read_capped(response: httpx.Response, bytes_max: int) -> bytes:
    """Stream response's raw body, decode gzip/deflate, and stop past bytes_max decoded."""
    assert bytes_max > 0
    decoder = _decoder(response.headers.get("content-encoding", "").strip().lower())
    chunks: list[bytes] = []
    raw_size = 0
    size = 0
    async for chunk in response.aiter_raw():
        raw_size += len(chunk)
        if raw_size > bytes_max:
            raise FetchError(f"body is larger than {bytes_max} bytes")
        piece = chunk if decoder is None else _inflate(decoder, chunk, bytes_max - size)
        size += len(piece)
        if size > bytes_max:
            raise FetchError(f"body is larger than {bytes_max} bytes")
        chunks.append(piece)
    if decoder is not None:
        tail = decoder.flush()
        if size + len(tail) > bytes_max:
            raise FetchError(f"body is larger than {bytes_max} bytes")
        chunks.append(tail)
    body = b"".join(chunks)
    assert len(body) <= bytes_max
    return body


def _gunzip_capped(body: bytes, bytes_max: int) -> bytes:
    """Gunzip a raw .gz body, refusing to inflate past bytes_max."""
    decoder = zlib.decompressobj(wbits=GZIP_WBITS)
    try:
        inflated = _inflate(decoder, body, bytes_max)
    except FetchError as error:
        raise FetchError(f"gzip body is larger than {bytes_max} bytes or corrupt") from error
    assert len(inflated) <= bytes_max
    return inflated


def _retry_after_s(value: str | None, now: datetime) -> float | None:
    """Parse Retry-After seconds or HTTP-date into seconds from now, capped at 24 hours."""
    assert now.tzinfo is not None
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        seconds = float(value)
    else:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - now).total_seconds()
    return min(max(seconds, 0.0), RETRY_AFTER_S_MAX)


class Fetcher:
    """The only way core code talks HTTP: SSRF-guarded, polite, robots-aware, size-capped.

    Use as an async context manager. persist=False writes no http_cache, robots_cache or
    meta retry_until rows (for check --online). resolver replaces DNS lookup, for tests.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        config: FetchConfig,
        *,
        persist: bool = True,
        resolver: Resolver | None = None,
    ) -> None:
        self._conn = conn
        self._config = config
        self._persist = persist
        self._allow = tuple(config.allow_private_hosts)
        self.user_agent = (
            config.user_agent or f"HackerBlog/{hackerblog.__version__} (+{PROJECT_URL})"
        )
        self._limiter = HostLimiter(config.min_delay_seconds, config.max_concurrent_hosts)
        self._backend = GuardedBackend(self._allow, resolver=resolver)
        self._robots: dict[str, RobotsPolicy] = {}
        self._robots_locks: dict[str, anyio.Lock] = {}
        self._retry_until: dict[str, datetime] = {}
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        assert self._client is None, "Fetcher is not re-entrant"
        self._client = httpx.AsyncClient(
            transport=_GuardedTransport(self._backend, self._config.max_concurrent_hosts),
            trust_env=False,
            follow_redirects=False,
            timeout=TIMEOUT,
            headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"},
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        assert self._client is not None
        await self._client.aclose()
        self._client = None

    async def get(self, url: str, *, kind: Kind, conditional: bool = False) -> Response:
        """GET url, following at most 5 redirects, each re-checked by policy and robots.

        conditional=True sends the stored ETag and Last-Modified for each hop's URL. Nothing
        is recorded until remember(), so a caller stores validators only once it has kept
        what the body held. Raises BlockedError or FetchError.
        """
        assert kind in BODY_BYTES_MAX
        current = url
        for _ in range(REDIRECTS_MAX + 1):
            if not await self.allowed(current):
                raise BlockedError("robots", f"robots.txt disallows {current}")
            validators = self._validators(current) if conditional else {}
            exchange = await self._exchange(current, validators, BODY_BYTES_MAX[kind])
            if exchange.location is None:
                return self._finish(exchange, kind)
            if urlsplit(current).scheme == "https" and urlsplit(exchange.location).scheme == "http":
                raise BlockedError(
                    "policy", f"{current} redirects to plain http {exchange.location}"
                )
            current = exchange.location
        raise FetchError(f"more than {REDIRECTS_MAX} redirects from {url}")

    async def allowed(self, url: str) -> bool:
        """Return whether robots.txt lets our User-Agent fetch url. Checks URL policy first."""
        self._check_policy(url)
        policy = await self._robots_policy(origin_of(url))
        return policy.allows(url, self.user_agent)

    async def robots_sitemaps(self, url: str) -> list[str]:
        """Return the Sitemap URLs listed in robots.txt for url's origin."""
        self._check_policy(url)
        policy = await self._robots_policy(origin_of(url))
        return policy.sitemaps()

    def _check_policy(self, url: str) -> None:
        assert url
        problem = url_policy_problem(url, self._allow)
        if problem is not None:
            raise BlockedError("policy", problem, "add the host to fetch.allow_private_hosts")
        assert urlsplit(url).hostname

    async def _robots_policy(self, origin: str) -> RobotsPolicy:
        self._check_retry(origin)
        cached = self._robots.get(origin)
        if cached is not None:
            return cached
        assert len(self._robots) < HostLimiter.HOSTS_MAX
        lock = self._robots_locks.setdefault(origin, anyio.Lock())
        async with lock:
            policy = self._robots.get(origin)
            if policy is None:
                policy = load_policy(self._conn, origin, datetime.now(UTC))
            if policy is None:
                policy = await self._fetch_robots(origin)
                if self._persist and policy.verdict != "disallow_all":
                    store_policy(self._conn, origin, policy, datetime.now(UTC))
            self._robots[origin] = policy
        self._limiter.raise_delay(origin, policy.delay_s(self.user_agent))
        return policy

    async def _fetch_robots(self, origin: str) -> RobotsPolicy:
        """Fetch origin's robots.txt: 2xx parsed, 4xx except 429 or too many redirects allow
        all, anything else (429, 5xx, timeout, network error, size cap) disallow all."""
        current = f"{origin}/robots.txt"
        try:
            for _ in range(REDIRECTS_MAX + 1):
                self._check_policy(current)
                exchange = await self._exchange(current, {}, ROBOTS_BYTES_MAX)
                if exchange.location is None:
                    break
                current = exchange.location
            else:
                return RobotsPolicy("allow_all", None)
        except FetchError:
            return RobotsPolicy("disallow_all", None)
        if 200 <= exchange.status < 300:
            return RobotsPolicy("parsed", exchange.body.decode("utf-8", errors="replace"))
        if 400 <= exchange.status < 500 and exchange.status != 429:
            return RobotsPolicy("allow_all", None)
        return RobotsPolicy("disallow_all", None)

    async def _exchange(self, url: str, headers: dict[str, str], bytes_max: int) -> _Exchange:
        """Send one GET through the host's politeness slot within the total time limit."""
        assert self._client is not None, "use Fetcher as an async context manager"
        assert bytes_max > 0
        origin = origin_of(url)
        self._check_retry(origin)
        async with self._limiter.slot(origin):
            try:
                with anyio.fail_after(TOTAL_S):
                    exchange = await self._send(self._client, url, headers, bytes_max)
            except httpx.HTTPError as error:
                raise FetchError(f"GET {url} failed: {error!r}") from error
            except TimeoutError as error:
                raise FetchError(f"GET {url} took longer than {TOTAL_S:.0f} s") from error
        if exchange.status in RETRY_STATUSES:
            self._hold(origin, exchange.headers.get("retry-after"))
        return exchange

    def _check_retry(self, origin: str) -> None:
        """Raise FetchError, sending nothing, while origin's Retry-After deadline is ahead."""
        now = datetime.now(UTC)
        until = self._retry_until.get(origin) or self._stored_retry_until(origin)
        if until is not None and until > now:
            remaining_s = min((until - now).total_seconds(), RETRY_AFTER_S_MAX)
            raise FetchError(
                f"{origin} asked to wait until {until.strftime(TIME_FORMAT)}",
                retry_after_s=remaining_s,
            )

    def _stored_retry_until(self, origin: str) -> datetime | None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (f"retry_until:{origin}",)
        ).fetchone()
        if row is None:
            return None
        try:
            until = datetime.fromisoformat(str(row[0]))
        except ValueError:
            return None
        return until if until.tzinfo is not None else until.replace(tzinfo=UTC)

    def _hold(self, origin: str, retry_after: str | None) -> None:
        """Record that origin answered 429 or 503: wait Retry-After, else an hour, at most a day."""
        assert origin in self._retry_until or len(self._retry_until) < HostLimiter.HOSTS_MAX
        now = datetime.now(UTC)
        seconds = _retry_after_s(retry_after, now)
        until = now + timedelta(seconds=RETRY_DEFAULT_S if seconds is None else seconds)
        assert until - now <= timedelta(seconds=RETRY_AFTER_S_MAX)
        self._retry_until[origin] = until
        if self._persist:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (f"retry_until:{origin}", until.strftime(TIME_FORMAT)),
            )

    async def _send(
        self, client: httpx.AsyncClient, url: str, headers: dict[str, str], bytes_max: int
    ) -> _Exchange:
        assert bytes_max > 0
        async with client.stream("GET", url, headers=headers) as response:
            status = response.status_code
            assert 100 <= status <= 599
            found = {key.lower(): value for key, value in response.headers.items()}
            location = found.get("location") if status in REDIRECT_STATUSES else None
            if location is not None:
                return _Exchange(url, status, found, b"", urljoin(url, location))
            if status in RETRY_STATUSES or status == 304:
                return _Exchange(url, status, found, b"", None)
            body = await _read_capped(response, bytes_max)
            return _Exchange(url, status, found, body, None)

    def _finish(self, exchange: _Exchange, kind: Kind) -> Response:
        assert exchange.location is None
        if exchange.status in RETRY_STATUSES:
            raise FetchError(
                f"{exchange.url} answered {exchange.status}",
                status=exchange.status,
                retry_after_s=_retry_after_s(
                    exchange.headers.get("retry-after"), datetime.now(UTC)
                ),
            )
        body = exchange.body
        gz_path = urlsplit(exchange.url).path.lower().endswith(".gz")
        if kind == "sitemap" and gz_path and body.startswith(GZIP_MAGIC):
            body = _gunzip_capped(body, BODY_BYTES_MAX[kind])
        assert exchange.status != 304 or body == b""
        return Response(exchange.url, exchange.status, body, exchange.headers)

    def _validators(self, url: str) -> dict[str, str]:
        assert url
        row = self._conn.execute(
            "SELECT etag, last_modified FROM http_cache WHERE url = ?", (url,)
        ).fetchone()
        if row is None:
            return {}
        etag, last_modified = row
        headers = {"If-None-Match": etag, "If-Modified-Since": last_modified}
        found = {key: value for key, value in headers.items() if value is not None}
        assert all(isinstance(value, str) for value in found.values())
        return found

    def remember(self, response: Response) -> None:
        """Record a 200 or 304 answer's validators in http_cache, unless persist is off.

        Call it in the same transaction that stores what the body held, so a failure later
        in the poll cannot leave validators behind that hide unstored entries.
        """
        assert response.status in {200, 304}, response.status
        if self._persist:
            self._store_validators(response)

    def _store_validators(self, exchange: Response) -> None:
        assert self._persist
        assert exchange.status in {200, 304}
        now = datetime.now(UTC).strftime(TIME_FORMAT)
        if exchange.status == 304:
            self._conn.execute(
                "UPDATE http_cache SET last_status = 304, fetched_at = ? WHERE url = ?",
                (now, exchange.url),
            )
            return
        self._conn.execute(
            "INSERT INTO http_cache (url, etag, last_modified, last_status, fetched_at) "
            "VALUES (?, ?, ?, 200, ?) ON CONFLICT (url) DO UPDATE SET etag = excluded.etag, "
            "last_modified = excluded.last_modified, last_status = 200, "
            "fetched_at = excluded.fetched_at",
            (
                exchange.url,
                exchange.headers.get("etag"),
                exchange.headers.get("last-modified"),
                now,
            ),
        )
