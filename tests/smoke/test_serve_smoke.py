import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

CLI = [sys.executable, "-c", "from hackerblog.cli.main import run; run()"]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def test_serve_answers_health_and_status(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "HACKERBLOG_HOME": str(tmp_path / "hb"),
        "HACKERBLOG_CONFIG": "",
        "HACKERBLOG_API_TOKEN": "",
    }
    init = subprocess.run(
        [*CLI, "init", "--output-dir", str(tmp_path / "out")],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert init.returncode == 0, init.stderr
    secrets = (tmp_path / "hb" / "secrets.env").read_text(encoding="utf-8")
    token = next(
        line.split("=", 1)[1]
        for line in secrets.splitlines()
        if line.startswith("HACKERBLOG_API_TOKEN=")
    )
    port = free_port()
    server = subprocess.Popen(
        [*CLI, "serve", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 30
        while True:
            assert server.poll() is None, "serve exited"
            try:
                health = httpx.get(f"{base}/v1/health", timeout=2)
                break
            except httpx.TransportError:
                assert time.monotonic() < deadline, "serve did not answer within 30 s"
                time.sleep(0.2)
        assert health.status_code == 200
        assert health.json()["db"] == "ok"
        status = httpx.get(f"{base}/v1/status", headers={"Authorization": f"Bearer {token}"})
        assert status.status_code == 200
    finally:
        server.terminate()
        output, _ = server.communicate(timeout=15)
    assert f"Serving on http://127.0.0.1:{port}" in output
