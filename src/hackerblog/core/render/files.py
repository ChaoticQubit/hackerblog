"""Output folders and file writes: slugged per-post folders, private modes, atomic replace.

Every path is built from validated slugs, resolved and asserted to stay inside the output
directory. Symlinks inside the output directory are refused, never followed.
"""

import contextlib
import json
import os
import re
import unicodedata
from pathlib import Path

SLUG_MAX = 60
SITE_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:T[0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?)?$")
NON_SLUG = re.compile(r"[^a-z0-9]+")
DIR_MODE = 0o700
FILE_MODE = 0o600
COLLISION_MAX = 1000


def post_slug(title: str) -> str:
    """Return title lowercased, ASCII-folded, non [a-z0-9] runs as '-', at most 60 chars."""
    folded = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = NON_SLUG.sub("-", folded.lower()).strip("-")[:SLUG_MAX].rstrip("-")
    result = slug or "post"
    assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", result)
    assert len(result) <= SLUG_MAX
    return result


def _private_dir(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"refusing symlink in output dir: {path}")
    path.mkdir(mode=DIR_MODE, exist_ok=True)
    path.chmod(DIR_MODE)
    assert path.is_dir() and not path.is_symlink()


def _owner(folder: Path) -> int | None:
    meta = folder / "post.json"
    if meta.is_symlink():
        raise ValueError(f"refusing symlink in output dir: {meta}")
    if not meta.is_file():
        return None
    with contextlib.suppress(ValueError, KeyError, TypeError):
        return int(json.loads(meta.read_text(encoding="utf-8"))["post"]["post_id"])
    return None


def post_dir(output_dir: Path, site_slug: str, date: str, title: str, post_id: int) -> Path:
    """Create and return <output_dir>/<site_slug>/<YYYY-MM-DD>-<post-slug>/ for post_id.

    A folder already holding another post's post.json gets -2, -3 ... appended. date is
    published_at or discovered_at. Raises ValueError on a bad slug or date, or a symlink.
    """
    if not SITE_SLUG.fullmatch(site_slug):
        raise ValueError(f"invalid site slug {site_slug!r}")
    day = DATE.fullmatch(date)
    if day is None:
        raise ValueError(f"invalid date {date!r}")
    root = output_dir.resolve()
    root.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    site = root / site_slug
    _private_dir(site)
    base = f"{day.group(1)}-{post_slug(title)}"
    for attempt in range(1, COLLISION_MAX):
        name = base if attempt == 1 else f"{base}-{attempt}"
        folder = site / name
        if folder.is_symlink():
            raise ValueError(f"refusing symlink in output dir: {folder}")
        owner = _owner(folder) if folder.exists() else None
        if not folder.exists() or owner == post_id or (owner is None and not any(folder.iterdir())):
            _private_dir(folder)
            resolved = folder.resolve()
            assert resolved.is_relative_to(root), f"{resolved} escapes {root}"
            assert resolved.parent == site.resolve()
            return resolved
    raise ValueError(f"too many posts named {base!r} in {site}")


def write_atomic(path: Path, data: str | bytes) -> None:
    """Write data to path through a private temp file in the same folder and os.replace.

    A reader never sees a partial file. A symlink at path is replaced, never followed;
    a symlinked parent folder is refused with ValueError.
    """
    parent = path.parent
    if parent.is_symlink():
        raise ValueError(f"refusing symlink in output dir: {parent}")
    assert parent.is_dir(), f"missing folder {parent}"
    assert path.name not in {"", ".", ".."}
    payload = data.encode("utf-8") if isinstance(data, str) else data
    temp = parent / f".{path.name}.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(temp, flags, FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), FILE_MODE)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    assert not path.is_symlink()
