"""Boot `hackerblog serve` in a throwaway home and scan it with nuclei.

Usage: python scripts/scan_service.py [--out DIR] <nuclei command...>, where the command is
for example `nuclei` or `docker run --rm --network host projectdiscovery/nuclei:latest`.
The home gets one site and a sample lesson under /files. Two scans run: every template at
medium, high or critical, then the misconfig and exposure templates at every severity.
Findings are written as JSON lines to DIR when given. Exits 1 on any medium, high or
critical finding from either scan.
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import cast

CLI = [sys.executable, "-c", "from hackerblog.cli.main import run; run()"]
GATE = ("medium", "high", "critical")
STARTUP_S = 30
LESSON = "example-org-writing/2026-11-06-sample/lesson.html"
SITE_BLOCK = '\n[[site]]\nname = "Sample"\nurl = "https://example.org/writing/"\n'
SCANS = {
    "gate": ["-severity", ",".join(GATE)],
    "misconfig-exposure": ["-tags", "misconfig,exposure"],
}


def free_port() -> int:
    """Return a TCP port that was free on 127.0.0.1 a moment ago."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def wait_healthy(url: str, server: subprocess.Popen[bytes]) -> None:
    """Poll url until it answers 200, failing if serve exits or STARTUP_S passes."""
    deadline = time.monotonic() + STARTUP_S
    while True:
        assert server.poll() is None, "hackerblog serve exited during startup"
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        assert time.monotonic() < deadline, f"no answer from {url} within {STARTUP_S} s"
        time.sleep(0.2)


def seed(home: Path, env: dict[str, str]) -> None:
    """Initialise the home, follow one site and place a sample lesson and notes in the output."""
    out = home / "out"
    subprocess.run([*CLI, "init", "--output-dir", str(out)], env=env, check=True)
    config = home / "hb" / "sites.toml"
    config.write_text(config.read_text(encoding="utf-8") + SITE_BLOCK, encoding="utf-8")
    lesson = out / LESSON
    lesson.parent.mkdir(parents=True)
    lesson.write_text("<!doctype html><title>Sample</title><h1>Sample lesson</h1>", "utf-8")
    (lesson.parent / "notes.md").write_text("# Sample notes\n", encoding="utf-8")


def scan(nuclei: list[str], targets: str, flags: list[str]) -> list[dict[str, object]]:
    """Run one nuclei scan over targets and return its findings."""
    result = subprocess.run(
        [*nuclei, "-u", targets, *flags, "-jsonl", "-silent", "-no-color"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def severity(finding: dict[str, object]) -> str:
    """Return a finding's severity in lowercase, or "unknown"."""
    info = finding.get("info")
    if not isinstance(info, dict):
        return "unknown"
    value: object = cast(dict[str, object], info).get("severity")
    return str(value).lower() if value else "unknown"


def main(argv: list[str]) -> int:
    """Seed, serve, run both scans, save findings and return the exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    parser.add_argument("nuclei", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    assert args.nuclei, "pass the nuclei command to run"
    results: dict[str, list[dict[str, object]]] = {}
    with tempfile.TemporaryDirectory() as scratch:
        home = Path(scratch)
        env = {**os.environ, "HOME": scratch, "HACKERBLOG_HOME": str(home / "hb")}
        env.pop("HACKERBLOG_CONFIG", None)
        seed(home, env)
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        server = subprocess.Popen([*CLI, "serve", "--port", str(port)], env=env)
        try:
            wait_healthy(f"{base}/v1/health", server)
            targets = f"{base},{base}/files/{LESSON}"
            for name, flags in SCANS.items():
                results[name] = scan(args.nuclei, targets, flags)
        finally:
            server.terminate()
            server.wait(timeout=15)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        for name, findings in results.items():
            lines = "".join(json.dumps(item) + "\n" for item in findings)
            (args.out / f"nuclei-{name}.jsonl").write_text(lines, encoding="utf-8")
    blocking = 0
    for name, findings in results.items():
        counts: dict[str, int] = {}
        for finding in findings:
            counts[severity(finding)] = counts.get(severity(finding), 0) + 1
            if severity(finding) in GATE:
                blocking += 1
                sys.stdout.write(f"{name}: {json.dumps(finding)[:400]}\n")
        sys.stderr.write(f"nuclei {name}: {len(findings)} finding(s) {counts}\n")
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
