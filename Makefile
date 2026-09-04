# litmus — development entry points. All tooling config lives in pyproject.toml.
.PHONY: install format lint fix typecheck test check soak hooks clean

install:  ## Sync venv (incl. dev deps) and install git hooks
	uv sync
	$(MAKE) hooks

format:  ## Auto-format
	uv run ruff format .

lint:  ## Lint with auto-fix
	uv run ruff check --fix .

fix: format lint  ## Auto-format + auto-fix lint

typecheck:  ## Pyright strict
	uv run pyright

test:  ## Run the framework self-test suite
	uv run pytest

check:  ## CI gate — verify only, no auto-fix. Must be green before push.
	uv run ruff format --check .
	uv run ruff check .
	uv run pyright
	uv run pytest
	uv run litmus-check examples

SOAK_RUNS ?= 8

soak:  ## Race gate — full suite SOAK_RUNS times; one green run proves nothing about timing
	@i=1; while [ $$i -le $(SOAK_RUNS) ]; do \
		echo "soak $$i/$(SOAK_RUNS)"; \
		uv run pytest -q || exit 1; \
		i=$$((i + 1)); \
	done

hooks:  ## Point git at the shared hooks (auto-lint on commit)
	git config core.hooksPath .githooks

clean:
	rm -rf .venv .pytest_cache .ruff_cache dist
	find . -type d -name __pycache__ -exec rm -rf {} +
