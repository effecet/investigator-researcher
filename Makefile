SKILL_NAME := researcher-investigator
SKILL_SRC  := skill
SKILL_DEST := $(HOME)/.claude/skills/$(SKILL_NAME)

# Override with `make test PYTHON=python3.12` if your default python3
# lacks the pyyaml dependency. CI (GitHub Actions) drives the same checks
# via uv + requirements-dev.txt — see .github/workflows/ci.yml.
PYTHON ?= python3

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

test:           ## Run pytest spec validation
	$(PYTHON) -m pytest tests/ -v

test-deps:      ## Install Python test dependencies (pyyaml + pytest)
	$(PYTHON) -m pip install -r requirements-dev.txt

lint:           ## Run ruff lint (matches CI: `ruff check .`)
	$(PYTHON) -m ruff check .

format-check:   ## Verify formatting (matches CI: `ruff format --check .`)
	$(PYTHON) -m ruff format --check .

format:         ## Auto-format python via ruff
	$(PYTHON) -m ruff format .

clean:          ## Remove caches
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@rm -rf .pytest_cache .ruff_cache
