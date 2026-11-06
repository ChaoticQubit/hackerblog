.PHONY: setup fmt lint typecheck test guard check clean

setup:
	uv sync --locked

fmt:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run pyright

test:
	uv run pytest -q

guard:
	@if grep -qi '^name = "litellm"' uv.lock; then echo "litellm found in uv.lock"; exit 1; fi
	@echo "guard: no litellm in uv.lock"

check: guard lint typecheck test

clean:
	rm -rf .pytest_cache .ruff_cache build dist
	find . -name __pycache__ -type d -prune -not -path './.venv/*' -exec rm -rf {} +
