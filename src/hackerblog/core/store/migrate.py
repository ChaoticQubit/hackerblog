"""SQLite connection setup and an ordered SQL migration runner keyed on PRAGMA user_version."""

import re
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def connect(db_path: Path) -> sqlite3.Connection:
    """Open db_path in WAL mode with foreign keys and a 5 s busy timeout.

    The connection is in autocommit mode; callers open transactions explicitly.
    """
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _migrations(directory: Path) -> list[tuple[int, Path]]:
    found = sorted(
        (int(match.group(1)), path)
        for path in directory.iterdir()
        if (match := MIGRATION_NAME.match(path.name))
    )
    numbers = [number for number, _ in found]
    assert numbers == list(range(1, len(found) + 1)), f"migrations not contiguous: {numbers}"
    return found


def migrate(conn: sqlite3.Connection, directory: Path = MIGRATIONS_DIR) -> int:
    """Apply every migration in directory newer than the database's user_version.

    Each migration and its version bump run in one transaction, so a failing file leaves
    the database at the previous version. Returns the number of migrations applied.
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    pending = [(number, path) for number, path in _migrations(directory) if number > current]
    for number, path in pending:
        sql = path.read_text(encoding="utf-8")
        try:
            conn.executescript(
                f"BEGIN IMMEDIATE;\n{sql}\n;PRAGMA user_version = {number};\nCOMMIT;"
            )
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        assert conn.execute("PRAGMA user_version").fetchone()[0] == number
    return len(pending)
