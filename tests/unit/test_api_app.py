import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from hackerblog.api.app import ServiceSettings, create_app
from hackerblog.api.security import is_loopback
from hackerblog.core.config import Config
from hackerblog.core.paths import resolve_paths
from hackerblog.core.render.lesson import LESSON_CSP
from hackerblog.core.store.migrate import connect, migrate

TOKEN = "test-token-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def settings(tmp_path: Path, bind_host: str = "127.0.0.1") -> ServiceSettings:
    home = tmp_path / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    paths = resolve_paths(home=home)
    conn = connect(paths.db)
    migrate(conn)
    conn.close()
    config = Config.model_validate({"version": 1, "output": {"dir": str(out)}})
    return ServiceSettings(token=TOKEN, paths=paths, config=config, bind_host=bind_host)


class Client:
    """Synchronous calls into the app in-process through httpx's ASGI transport."""

    def __init__(self, app: FastAPI, base_url: str = "http://127.0.0.1:8765") -> None:
        self.app = app
        self.base_url = base_url

    def request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        async def send() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url=self.base_url) as http:
                return await http.request(
                    method, path, headers=headers, params=params, content=content
                )

        return asyncio.run(send())

    def get(
        self, path: str, headers: dict[str, str] | None = None, params: dict[str, str] | None = None
    ) -> httpx.Response:
        return self.request("GET", path, headers=headers, params=params)

    def post(
        self, path: str, headers: dict[str, str] | None = None, content: bytes | None = None
    ) -> httpx.Response:
        return self.request("POST", path, headers=headers, content=content)


def client(tmp_path: Path, bind_host: str = "127.0.0.1") -> Client:
    return Client(create_app(settings(tmp_path, bind_host)))


def test_health_needs_no_token(tmp_path: Path) -> None:
    response = client(tmp_path).get("/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["scheduler"] == "disabled"


def test_deep_health_probes_a_write(tmp_path: Path) -> None:
    response = client(tmp_path).get("/v1/health", params={"deep": "true"})
    assert response.status_code == 200
    assert response.json()["db"] == "ok"


def test_health_reports_broken_database(tmp_path: Path) -> None:
    app_settings = settings(tmp_path)
    app_settings.paths.db.unlink()
    app_settings.paths.db.mkdir()
    response = Client(create_app(app_settings)).get("/v1/health")
    assert response.status_code == 503
    assert response.json()["db"] == "error"


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}, {"Authorization": "Basic x"}],
)
def test_status_rejects_missing_or_wrong_token(tmp_path: Path, headers: dict[str, str]) -> None:
    response = client(tmp_path).get("/v1/status", headers=headers)
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["code"] == "unauthorized"


def test_status_with_token(tmp_path: Path) -> None:
    response = client(tmp_path).get("/v1/status", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["scheduler"] == {"state": "disabled", "jobs": []}
    assert body["sites"] == 0


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1.evil.example", "localhost.:80"])
def test_untrusted_host_is_refused(tmp_path: Path, host: str) -> None:
    response = client(tmp_path).get("/v1/health", headers={"Host": host})
    assert response.status_code == 400
    assert response.json()["code"] == "untrusted_host"


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost", "[::1]:8765"])
def test_loopback_hosts_are_trusted(tmp_path: Path, host: str) -> None:
    assert client(tmp_path).get("/v1/health", headers={"Host": host}).status_code == 200


def test_unknown_route_is_a_problem(tmp_path: Path) -> None:
    response = client(tmp_path).get("/v1/nope", headers=AUTH)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == 404
    assert body["code"] == "not_found"


def test_large_body_is_refused(tmp_path: Path) -> None:
    response = client(tmp_path).post("/v1/status", headers=AUTH, content=b"x" * (65 * 1024))
    assert response.status_code == 413
    assert response.json()["code"] == "too_large"


def test_no_cors_headers(tmp_path: Path) -> None:
    response = client(tmp_path).get("/v1/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers


def write_output(tmp_path: Path) -> Path:
    out = tmp_path / "out" / "site" / "2026-11-06-post"
    out.mkdir(parents=True)
    (out / "lesson.html").write_text("<h1>lesson</h1>", encoding="utf-8")
    (out / "notes.md").write_text("# notes", encoding="utf-8")
    (out / "post.json").write_text("{}", encoding="utf-8")
    (out / "evil.py").write_text("x", encoding="utf-8")
    (tmp_path / "out" / ".hidden.html").write_text("x", encoding="utf-8")
    (tmp_path / "secret.html").write_text("outside", encoding="utf-8")
    (out / "link.html").symlink_to(tmp_path / "secret.html")
    return out


def test_files_serves_lesson_with_security_headers(tmp_path: Path) -> None:
    test_client = client(tmp_path)
    write_output(tmp_path)
    response = test_client.get("/files/site/2026-11-06-post/lesson.html")
    assert response.status_code == 200
    assert response.text == "<h1>lesson</h1>"
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "connect-src 'none'" in csp
    assert "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox" in csp
    assert "allow-same-origin" not in csp
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cross-origin-opener-policy"] == "same-origin"


def test_files_serves_lesson_with_the_lesson_csp_so_its_script_runs(tmp_path: Path) -> None:
    test_client = client(tmp_path)
    write_output(tmp_path)
    response = test_client.get("/files/site/2026-11-06-post/lesson.html")
    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert csp.startswith(f"{LESSON_CSP}; ")
    assert "sandbox allow-scripts" in csp and "allow-same-origin" not in csp


def test_files_serves_other_html_with_scripts_off(tmp_path: Path) -> None:
    test_client = client(tmp_path)
    out = write_output(tmp_path)
    (out / "other.html").write_text("<p>x</p>", encoding="utf-8")
    response = test_client.get("/files/site/2026-11-06-post/other.html")
    assert response.status_code == 200
    assert "script-src 'none'" in response.headers["content-security-policy"]


def test_files_serves_notes_as_plain_text(tmp_path: Path) -> None:
    test_client = client(tmp_path)
    write_output(tmp_path)
    response = test_client.get("/files/site/2026-11-06-post/notes.md")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["x-frame-options"] == "DENY"
    json_response = test_client.get("/files/site/2026-11-06-post/post.json")
    assert json_response.headers["content-type"].startswith("application/json")


@pytest.mark.parametrize(
    "path",
    [
        "/files/../secret.html",
        "/files/..%2fsecret.html",
        "/files/site/2026-11-06-post/..%2f..%2f..%2fsecret.html",
        "/files/%2e%2e/secret.html",
        "/files/site/2026-11-06-post/link.html",
        "/files/.hidden.html",
        "/files/site/2026-11-06-post/evil.py",
        "/files/site/2026-11-06-post/missing.html",
        "/files/site",
        "/files/site/2026-11-06-post/lesson.html%00.md",
        "/files//etc/passwd",
    ],
)
def test_files_refuses_traversal_and_other_files(tmp_path: Path, path: str) -> None:
    test_client = client(tmp_path)
    write_output(tmp_path)
    response = test_client.get(path)
    assert response.status_code == 404
    assert "outside" not in response.text


def test_files_need_token_when_not_bound_to_loopback(tmp_path: Path) -> None:
    test_client = Client(
        create_app(settings(tmp_path, bind_host="192.168.1.20")), "http://192.168.1.20:8765"
    )
    write_output(tmp_path)
    path = "/files/site/2026-11-06-post/lesson.html"
    assert test_client.get(path).status_code == 401
    assert test_client.get(path, headers=AUTH).status_code == 200


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("127.0.0.2", True),
        ("::1", True),
        ("[::1]", True),
        ("localhost", True),
        ("127.example.com", False),
        ("0.0.0.0", False),
        ("192.168.1.20", False),
    ],
)
def test_is_loopback(host: str, expected: bool) -> None:
    assert is_loopback(host) is expected


BASELINE_HEADERS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "cross-origin-embedder-policy": "require-corp",
    "x-permitted-cross-domain-policies": "none",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}


@pytest.mark.parametrize(
    ("path", "status"),
    [("/v1/health", 200), ("/v1/nope", 404), ("/", 404)],
)
def test_every_response_has_baseline_security_headers(
    tmp_path: Path, path: str, status: int
) -> None:
    response = client(tmp_path).get(path, headers=AUTH)
    assert response.status_code == status
    for name, value in BASELINE_HEADERS.items():
        assert response.headers.get(name) == value, name
    assert response.headers["x-frame-options"] == "DENY"
    csp = response.headers["content-security-policy"]
    assert csp == "default-src 'none'; frame-ancestors 'none'"


def test_refused_hosts_also_get_security_headers(tmp_path: Path) -> None:
    response = client(tmp_path).get("/v1/health", headers={"Host": "evil.example"})
    assert response.status_code == 400
    assert response.headers["x-content-type-options"] == "nosniff"


def test_lesson_keeps_its_own_csp_and_allows_same_origin_framing(tmp_path: Path) -> None:
    test_client = client(tmp_path)
    write_output(tmp_path)
    response = test_client.get("/files/site/2026-11-06-post/lesson.html")
    assert "sandbox allow-scripts" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_interactive_docs_are_not_served(tmp_path: Path, path: str) -> None:
    assert client(tmp_path).get(path).status_code == 404


def test_openapi_contract_needs_token(tmp_path: Path) -> None:
    test_client = client(tmp_path)
    assert test_client.get("/v1/openapi.json").status_code == 401
    response = test_client.get("/v1/openapi.json", headers=AUTH)
    assert response.status_code == 200
    assert "/v1/health" in response.json()["paths"]
