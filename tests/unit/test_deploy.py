import importlib.util
import re
import shutil
import sqlite3
import subprocess
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
UNITS = (
    "hackerblog.service",
    "hackerblog-keepalive.service",
    "hackerblog-backup.service",
    "hackerblog-backup.timer",
)
BASH = shutil.which("bash") or "bash"
needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")


def load_keepalive() -> ModuleType:
    spec = importlib.util.spec_from_file_location("keepalive", DEPLOY / "keepalive.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_keepalive_reads_memtotal_in_bytes(tmp_path: Path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemFree:  100 kB\nMemTotal:       12010000 kB\n", encoding="ascii")
    assert load_keepalive().mem_total_bytes(meminfo) == 12010000 * 1024


def test_keepalive_rejects_meminfo_without_memtotal(tmp_path: Path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemFree: 100 kB\n", encoding="ascii")
    with pytest.raises(ValueError, match="no MemTotal"):
        load_keepalive().mem_total_bytes(meminfo)


def test_keepalive_holds_a_quarter_by_default_and_caps_at_half() -> None:
    keepalive = load_keepalive()
    total = 12 * 2**30
    assert keepalive.ballast_bytes(total, keepalive.FRACTION_DEFAULT) == 3 * 2**30
    assert keepalive.FRACTION_DEFAULT * 100 > 20
    for bad in (0.0, -0.1, 0.51, 1.0):
        with pytest.raises(ValueError, match="fraction"):
            keepalive.ballast_bytes(total, bad)


def test_deploy_files_carry_no_comments() -> None:
    for path in (entry for entry in DEPLOY.iterdir() if entry.is_file()):
        lines = path.read_text(encoding="utf-8").splitlines()
        body = lines[1:] if lines and lines[0].startswith("#!") else lines
        commented = [line for line in body if line.lstrip().startswith("#")]
        assert not commented, f"{path.name} has comment lines: {commented}"


def test_caddyfile_template_has_each_placeholder_once() -> None:
    template = (DEPLOY / "Caddyfile.template").read_text(encoding="utf-8")
    for placeholder in ("__ACME_EMAIL__", "__DOMAIN__", "__WEB_USER__", "__WEB_HASH__"):
        assert template.count(placeholder) == 1, placeholder
    assert "header_up Host 127.0.0.1:8765" in template
    assert "basic_auth @browser" in template


def test_scripts_install_every_unit_that_exists() -> None:
    for script in ("setup.sh", "update.sh"):
        text = (DEPLOY / script).read_text(encoding="utf-8")
        match = re.search(r"readonly units=\(([^)]*)\)", text)
        assert match is not None, script
        assert tuple(match.group(1).split()) == UNITS
    for unit in UNITS:
        assert (DEPLOY / unit).is_file(), unit


@needs_bash
def test_setup_dry_run_prints_the_plan_and_rejects_bad_input() -> None:
    script = str(DEPLOY / "setup.sh")
    base = ["--email", "me@example.com", "--repo", "git@github.com:o/hackerblog.git", "--dry-run"]
    done = subprocess.run(
        [BASH, script, "--domain", "hb.example.org", *base],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    for expected in (
        "would run: useradd",
        "sync --locked --no-dev",
        "hackerblog init --output-dir /home/hackerblog/HackerBlog",
        "would write: /etc/caddy/Caddyfile",
        "netfilter-persistent save",
        "would write: /etc/ssh/sshd_config.d/10-hackerblog.conf",
        "https://hb.example.org/files",
    ):
        assert expected in done.stdout, expected
    bad = subprocess.run(
        [BASH, script, "--domain", "bad domain", *base],
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad.returncode == 1
    assert "--domain must be a DNS name" in bad.stderr


@needs_bash
@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI is not installed")
def test_backup_copies_database_and_output_and_keeps_the_newest(tmp_path: Path) -> None:
    home, output, backups = tmp_path / "home", tmp_path / "HackerBlog", tmp_path / "backups"
    for folder in (home, output / "post-a", backups):
        folder.mkdir(parents=True)
    with sqlite3.connect(home / "hackerblog.db") as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (x)")
        conn.executemany("INSERT INTO t VALUES (?)", [(1,), (2,)])
    (output / "post-a" / "notes.html").write_text("hi", encoding="utf-8")
    for stamp in ("20260101T000000Z", "20260102T000000Z"):
        (backups / stamp).mkdir()
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        "HACKERBLOG_HOME": str(home),
        "HACKERBLOG_OUTPUT_DIR": str(output),
        "HACKERBLOG_BACKUP_DIR": str(backups),
        "HACKERBLOG_BACKUP_KEEP": "2",
        "HACKERBLOG_BACKUP_REQUIRE_MOUNT": "0",
    }
    done = subprocess.run(
        [BASH, str(DEPLOY / "backup.sh")], capture_output=True, text=True, env=env, check=False
    )
    assert done.returncode == 0, done.stderr
    kept = sorted(path.name for path in backups.iterdir())
    assert len(kept) == 2 and kept[0] == "20260102T000000Z", kept
    newest = backups / kept[1]
    assert sorted(path.name for path in newest.iterdir()) == ["hackerblog.db", "output.tar.gz"]
    with sqlite3.connect(newest / "hackerblog.db") as conn:
        assert conn.execute("SELECT count(*) FROM t").fetchone() == (2,)
    with tarfile.open(newest / "output.tar.gz") as archive:
        assert "HackerBlog/post-a/notes.html" in archive.getnames()
    refused = subprocess.run(
        [BASH, str(DEPLOY / "backup.sh")],
        capture_output=True,
        text=True,
        env={**env, "HACKERBLOG_BACKUP_KEEP": "zero"},
        check=False,
    )
    assert refused.returncode == 1
    assert "positive integer" in refused.stderr
