# amd-gputools — the make targets are the interface.

PYTHON ?= python3

.PHONY: help install lint test check ssh sync scaffold

help:  ## List targets
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | expand -t22

install:  ## Install dependencies into the system python3 (no virtualenv)
	$(PYTHON) -m pip install -r requirements.txt

ssh:  ## Open a shell on the GPU droplet (address resolved from the API)
	./ssh-droplet.sh

scaffold:  ## Re-scaffold the droplet: apt, docker, GPU bind, vLLM image
	./scaffold-droplet.sh

sync:  ## Copy the working tree to the droplet at /opt/amd-gputools
	tar czf - --exclude=.git --exclude=.env --exclude=__pycache__ \
	    --exclude=.ruff_cache --exclude=run . \
	  | ./ssh-droplet.sh 'mkdir -p /opt/amd-gputools && tar xzf - -C /opt/amd-gputools'

lint:  ## Lint and format-check every python file in the project
	ruff format --check .
	ruff check .
	shellcheck ./*.sh ./vllm/*.sh ./scaffold/*.sh

test:  ## Run the offline unit tests — unittest, never pytest
	$(PYTHON) -m unittest discover -s tests -v

check: lint test  ## What CI would run
