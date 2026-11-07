import json
import stat
from pathlib import Path

import pytest

from hackerblog.core.render.files import post_dir, post_slug, write_atomic


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def claim(directory: Path, post_id: int) -> None:
    write_atomic(directory / "post.json", json.dumps({"post": {"post_id": post_id}}))


def test_post_slug_folds_and_trims() -> None:
    assert post_slug("Smashing the State Machine!") == "smashing-the-state-machine"
    assert post_slug("Ça déjà vu: naïve café") == "ca-deja-vu-naive-cafe"
    assert post_slug("../../etc/passwd") == "etc-passwd"
    assert post_slug("💥🔥") == "post"
    long = post_slug("word " * 40)
    assert len(long) <= 60 and not long.endswith("-")


def test_post_dir_layout_and_permissions(tmp_path: Path) -> None:
    out = tmp_path / "out"
    folder = post_dir(out, "portswigger-net", "2026-11-03T09:00:00Z", "Smashing the state", 1)
    assert folder == (out / "portswigger-net" / "2026-11-03-smashing-the-state").resolve()
    assert folder.is_dir()
    assert mode(folder) == 0o700
    assert mode(folder.parent) == 0o700


def test_post_dir_collision_with_other_post(tmp_path: Path) -> None:
    first = post_dir(tmp_path, "site", "2026-11-03", "Same title", 1)
    claim(first, 1)
    assert post_dir(tmp_path, "site", "2026-11-03", "Same title", 1) == first
    second = post_dir(tmp_path, "site", "2026-11-03", "Same title", 2)
    assert second.name == "2026-11-03-same-title-2"
    claim(second, 2)
    third = post_dir(tmp_path, "site", "2026-11-03", "Same title", 3)
    assert third.name == "2026-11-03-same-title-3"


def test_traversal_titles_stay_in_tree(tmp_path: Path) -> None:
    for title in ("../../x", "/etc/passwd", "..", "a/../../b", "\x00..\\..\\x"):
        folder = post_dir(tmp_path, "site", "2026-01-01", title, 1)
        assert folder.is_relative_to(tmp_path.resolve())
        assert folder.parent == (tmp_path / "site").resolve()


@pytest.mark.parametrize("slug", ["../x", "a/b", "", "A", ".hidden"])
def test_bad_site_slug_rejected(tmp_path: Path, slug: str) -> None:
    with pytest.raises(ValueError, match="site slug"):
        post_dir(tmp_path, slug, "2026-01-01", "t", 1)


def test_bad_date_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="date"):
        post_dir(tmp_path, "site", "../2026", "t", 1)


def test_symlinked_site_dir_refused(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    (out / "site").symlink_to(elsewhere)
    with pytest.raises(ValueError, match="symlink"):
        post_dir(out, "site", "2026-01-01", "t", 1)


def test_write_atomic_replaces_with_private_file(tmp_path: Path) -> None:
    target = tmp_path / "notes.md"
    write_atomic(target, "one")
    write_atomic(target, "two")
    assert target.read_text() == "two"
    assert mode(target) == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == ["notes.md"]


def test_write_atomic_does_not_follow_symlink(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    link = tmp_path / "lesson.html"
    link.symlink_to(victim)
    write_atomic(link, "new")
    assert victim.read_text() == "keep"
    assert not link.is_symlink()
    assert link.read_text() == "new"


def test_write_atomic_refuses_symlinked_parent(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real)
    with pytest.raises(ValueError, match="symlink"):
        write_atomic(linked / "a.txt", "x")
