"""Problem responses, bearer-token auth, Trusted Host and body-size limits for the API."""

import hmac
import ipaddress
from collections.abc import Awaitable, Callable, MutableMapping
from http import HTTPStatus
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

BODY_BYTES_MAX = 64 * 1024
BASELINE_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"cross-origin-embedder-policy", b"require-corp"),
    (b"x-permitted-cross-domain-policies", b"none"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
)
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    413: "too_large",
    422: "validation",
}


def problem(
    status: int, code: str, detail: str = "", headers: dict[str, str] | None = None
) -> JSONResponse:
    """Return an RFC 9457 application/problem+json response with a CLI-style code."""
    assert 400 <= status <= 599
    body = {
        "type": "about:blank",
        "title": HTTPStatus(status).phrase,
        "status": status,
        "detail": detail,
        "code": code,
    }
    return JSONResponse(body, status, headers=headers, media_type="application/problem+json")


def is_loopback(host: str) -> bool:
    """Tell whether a bind address only accepts connections from this machine."""
    name = host.lower().strip("[]")
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def require_token(request: Request) -> None:
    """Raise 401 unless the request carries `Authorization: Bearer <api token>`."""
    expected: str = request.app.state.settings.token
    assert expected
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    supplied = value.strip().encode()
    if scheme.lower() != "bearer" or not hmac.compare_digest(supplied, expected.encode()):
        raise HTTPException(401, "a valid bearer token is required", {"WWW-Authenticate": "Bearer"})


def host_name(header: str) -> str | None:
    """Return the lowercase host name of a Host header without port or brackets."""
    try:
        return urlsplit(f"//{header}").hostname
    except ValueError:
        return None


class TrustedHost:
    """Refuse requests whose Host header is not an allowed name, blocking DNS rebinding."""

    def __init__(self, app: ASGIApp, allowed: frozenset[str]) -> None:
        assert allowed
        self.app = app
        self.allowed = allowed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"}:
            name = host_name(Headers(scope=scope).get("host", ""))
            if name not in self.allowed:
                response = problem(400, "untrusted_host", "the Host header is not allowed")
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class BodyLimit:
    """Refuse request bodies larger than BODY_BYTES_MAX with 413, before routing."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        length = Headers(scope=scope).get("content-length")
        if length is not None and (not length.isdigit() or int(length) > BODY_BYTES_MAX):
            await problem(413, "too_large", "the request body is too large")(scope, receive, send)
            return
        messages: list[Message] = []
        size = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                break
            size += len(message.get("body", b""))
            if size > BODY_BYTES_MAX:
                await problem(413, "too_large", "the request body is too large")(
                    scope, receive, send
                )
                return
            if not message.get("more_body", False):
                break

        async def replay() -> Message:
            return messages.pop(0) if messages else await receive()

        await self.app(scope, replay, send)


class SecurityHeaders:
    """Add the baseline security headers to every HTTP response that does not set its own."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers: list[tuple[bytes, bytes]] = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers.extend(pair for pair in BASELINE_HEADERS if pair[0] not in present)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
