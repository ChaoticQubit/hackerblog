import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "hackerblog"
CORE = SRC / "core"
AGNO_BACKEND = CORE / "llm" / "agno_backend.py"
CORE_FORBIDDEN = {"typer", "click", "fastapi", "starlette", "mcp", "uvicorn"}


def imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
            if node.module.startswith("hackerblog."):
                roots.add(".".join(node.module.split(".")[:2]))
    return roots


def modules(root: Path) -> list[Path]:
    found = sorted(root.rglob("*.py"))
    assert found, f"no modules under {root}"
    return found


@pytest.mark.parametrize("path", modules(CORE), ids=lambda p: str(p.relative_to(SRC)))
def test_core_never_imports_adapters(path: Path) -> None:
    roots = imported_roots(path)
    assert not roots & CORE_FORBIDDEN
    assert "hackerblog.cli" not in roots
    assert "hackerblog.api" not in roots


@pytest.mark.parametrize("path", modules(SRC), ids=lambda p: str(p.relative_to(SRC)))
def test_only_agno_backend_imports_agno(path: Path) -> None:
    if path == AGNO_BACKEND:
        return
    assert "agno" not in imported_roots(path)


def test_imported_roots_sees_both_import_forms(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("import typer.core\nfrom agno.agent import Agent\n", encoding="utf-8")
    assert {"typer", "agno"} <= imported_roots(sample)


def test_lockfile_has_no_litellm() -> None:
    lock = (SRC.parents[1] / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "litellm"' not in lock
