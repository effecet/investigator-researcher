SKILL_NAME := researcher-investigator
SKILL_SRC  := skill
SKILL_DEST := $(HOME)/.claude/skills/$(SKILL_NAME)

# Interpreter for pytest. tests/ imports pyyaml, which a system python3 may
# lack (Homebrew's python3.14 does). `test` probes the candidates below and
# uses the first one that can import yaml, so the common case needs no flags.
# Force a specific one with `make test PYTHON=python3.12`: an explicit
# PYTHON is the only candidate, so a wrong one fails instead of being swapped.
# CI (GitHub Actions) drives the same checks via uv + requirements-dev.txt —
# see .github/workflows/ci.yml.
PYTHON ?= python3
PYTHON_CANDIDATES := $(if $(filter command line environment,$(origin PYTHON)),$(PYTHON),$(PYTHON) python3.13 python3.12 python3.11)

# Prefer a ruff binary on PATH, fall back to `-m ruff` for environments that
# only ship the module. `python3 -m ruff` alone breaks whenever ruff is
# installed as a standalone binary rather than into the default interpreter.
# CI runs `uv run ruff` with a pinned version, so a local ruff of another
# version can disagree with it.
RUFF ?= $(shell command -v ruff 2>/dev/null || echo "$(PYTHON) -m ruff")

.PHONY: help install uninstall test test-deps lint format-check format clean

help:           ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:        ## Copy skill/ to ~/.claude/skills/researcher-investigator/
	@mkdir -p "$(SKILL_DEST)"
	@rsync -a --delete "$(SKILL_SRC)/" "$(SKILL_DEST)/"
	@echo "Installed $(SKILL_NAME) → $(SKILL_DEST)"

uninstall:      ## Remove installed skill copy
	@rm -rf "$(SKILL_DEST)"
	@echo "Removed $(SKILL_DEST)"

test:           ## Run pytest spec validation (auto-picks an interpreter with pyyaml)
	@for p in $(PYTHON_CANDIDATES); do \
	  if command -v "$$p" >/dev/null 2>&1 && "$$p" -c 'import yaml' >/dev/null 2>&1; then \
	    echo "==> using $$p"; \
	    exec "$$p" -m pytest tests/ -v; \
	  fi; \
	done; \
	echo "make: no Python with pyyaml found."; \
	echo "      tried: $(PYTHON_CANDIDATES)"; \
	echo "      fix:   make test-deps PYTHON=<venv python>   (or: make test PYTHON=/path/to/python-with-pyyaml)"; \
	exit 1

test-deps:      ## Install Python test dependencies (pyyaml + pytest)
	$(PYTHON) -m pip install -r requirements-dev.txt

lint:           ## Run ruff lint (matches CI: `ruff check .`)
	$(RUFF) check .

format-check:   ## Verify formatting (matches CI: `ruff format --check .`)
	$(RUFF) format --check .

format:         ## Auto-format python via ruff
	$(RUFF) format .

clean:          ## Remove caches
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@rm -rf .pytest_cache .ruff_cache
