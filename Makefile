# amd-gputools — the make targets are the interface.

PYTHON ?= python3

.PHONY: help install lint test check

help:  ## List targets
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | expand -t22

install:  ## Install dependencies into the system python3 (no virtualenv)
	$(PYTHON) -m pip install -r requirements.txt

lint:  ## Lint and format-check every python file in the project
	ruff format --check .
	ruff check .

test:  ## Run the offline unit tests — unittest, never pytest
	$(PYTHON) -m unittest discover -s tests -v

check: lint test  ## What CI would run
