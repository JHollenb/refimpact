.DEFAULT_GOAL := help
ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
PYTHON ?= python3
HOST ?= 0.0.0.0
PORT ?= 8787

.PHONY: help setup run start status clean refresh update test

help:
	@printf '%s\n' \
	  'make setup    Install locked dependencies (first run).' \
	  'make run      Serve in the foreground; reachable on your Wi-Fi.' \
	  'make start    Serve in the background; logs: .cache/server.log.' \
	  'make status   Show running Ref Impact server instances.' \
	  'make clean    Stop this checkout server instances; preserve data.' \
	  'make refresh  Reuse stored sources and transformations (offline when cached).' \
	  'make update   Download upstream revisions and generate updated results.' \
	  'make test     Run the test suite.' \
	  'Override HOST/PORT, e.g. make start HOST=127.0.0.1 PORT=8788.'

setup:
	cd "$(ROOT)" && uv sync --locked --cache-dir .cache/uv

run start:
	@$(PYTHON) "$(ROOT)/scripts/server.py" $@ --host "$(HOST)" --port "$(PORT)"

status clean:
	@$(PYTHON) "$(ROOT)/scripts/server.py" $@

refresh:
	cd "$(ROOT)" && .venv/bin/refimpact refresh

update:
	cd "$(ROOT)" && .venv/bin/refimpact refresh --download

test:
	cd "$(ROOT)" && .venv/bin/python -m pytest -q
