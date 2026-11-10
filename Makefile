.PHONY: setup fmt lint typecheck test test-offline test-live guard check check-live scan nuclei-templates clean

NUCLEI_TEMPLATES_VERSION := 10.5.1
NUCLEI_TEMPLATES_SHA256 := 30fe1cfaced4635f886e4a2673641bba86998dceec28e468274524e789aa56eb
NUCLEI_TEMPLATES := .local-state/nuclei-templates-$(NUCLEI_TEMPLATES_VERSION)
NUCLEI_TARBALL := .local-state/nuclei-templates-$(NUCLEI_TEMPLATES_VERSION).tar.gz

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

PYTEST_ARGS ?=

test:
	uv run pytest -q $(PYTEST_ARGS)

test-offline:
	uv run pytest -q -m "not live" $(PYTEST_ARGS)

test-live:
	uv run pytest -q -m live $(PYTEST_ARGS)

guard:
	@if grep -qi '^name = "litellm"' uv.lock; then echo "litellm found in uv.lock"; exit 1; fi
	@echo "guard: no litellm in uv.lock"

check: guard lint typecheck test-offline

check-live: test-live

nuclei-templates: $(NUCLEI_TEMPLATES)

$(NUCLEI_TEMPLATES):
	mkdir -p .local-state
	curl -fsSL -o $(NUCLEI_TARBALL) https://github.com/projectdiscovery/nuclei-templates/archive/refs/tags/v$(NUCLEI_TEMPLATES_VERSION).tar.gz
	echo "$(NUCLEI_TEMPLATES_SHA256)  $(NUCLEI_TARBALL)" | shasum -a 256 -c -
	mkdir -p $@.tmp
	tar -xzf $(NUCLEI_TARBALL) -C $@.tmp --strip-components 1
	mv $@.tmp $@

scan: $(NUCLEI_TEMPLATES)
	uv run python scripts/scan_service.py --out .local-state/nuclei nuclei -t $(NUCLEI_TEMPLATES) -duc

clean:
	rm -rf .pytest_cache .ruff_cache build dist
	find . -name __pycache__ -type d -prune -not -path './.venv/*' -exec rm -rf {} +
