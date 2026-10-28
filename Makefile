# Makefile for the Artifactory -> GitHub migration toolkit.
#
# Most targets accept TYPE=all|pypi|docker|npm|data (default: all), e.g.
#   make inventory TYPE=pypi
#   make dry-run                 # preview the whole pipeline, no changes
#   make publish-npm             # publish just one type
#
# Credentials come from the environment (never committed):
#   ARTIFACTORY_TOKEN, ARTIFACTORY_USER, GH_TOKEN  (see `make auth-check`).

PY   ?= python
TYPE ?= all

.DEFAULT_GOAL := help

.PHONY: help install lint check auth-check discover inventory dry-run publish verify cleanup report index clean distclean

help:  ## Show this help
	@grep -E '^[a-zA-Z_%-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}'

install:  ## Install Python dependencies
	$(PY) -m pip install -r requirements.txt

lint:  ## Lint with ruff (same config as CI; `pip install ruff` first)
	ruff check artmig migrate.py

check:  ## Byte-compile the toolkit (quick sanity check)
	$(PY) -m compileall -q artmig migrate.py

auth-check:  ## Report whether required credentials are available
	@if [ -n "$$ARTIFACTORY_TOKEN" ]; then echo "ARTIFACTORY_TOKEN : set"; else echo "ARTIFACTORY_TOKEN : MISSING"; fi
	@if [ -n "$$ARTIFACTORY_USER" ];  then echo "ARTIFACTORY_USER  : set"; else echo "ARTIFACTORY_USER  : (optional, unset)"; fi
	@gh auth status >/dev/null 2>&1 && echo "gh auth           : logged in" || echo "gh auth           : NOT logged in (run 'gh auth login')"
	@docker info >/dev/null 2>&1 && echo "docker daemon     : running" || echo "docker daemon     : not reachable"

discover:  ## Live discovery report (md/html/pdf) + coverage gate (fails if unhandled)
	$(PY) migrate.py discover

inventory:  ## Crawl Artifactory into manifests/ (read-only). Use TYPE=...
	$(PY) migrate.py inventory --type $(TYPE)

dry-run:  ## Preview the full pipeline without changing anything. Use TYPE=...
	$(PY) migrate.py run --type $(TYPE) --dry-run

publish:  ## Download + transform + publish to GitHub. Use TYPE=...
	$(PY) migrate.py publish --type $(TYPE)

verify:  ## Probe deployed artifacts (HTTP/sha256/npm/docker). Use TYPE=...
	$(PY) migrate.py verify --type $(TYPE)

cleanup:  ## ROLLBACK: delete migrated releases/packages (prompts). Use TYPE=...
	$(PY) migrate.py cleanup --type $(TYPE)

report:  ## Generate the migration report (HTML + Markdown) into report/
	$(PY) migrate.py report

index:  ## Regenerate the PEP 503 index from manifests/pypi-inventory.json
	$(PY) migrate.py transform --type pypi

inventory-%:  ## Inventory one type, e.g. make inventory-docker
	$(PY) migrate.py inventory --type $*

dryrun-%:  ## Dry-run publish one type, e.g. make dryrun-npm
	$(PY) migrate.py publish --type $* --dry-run

publish-%:  ## Publish one type, e.g. make publish-npm
	$(PY) migrate.py publish --type $*

cleanup-%:  ## Rollback one type, e.g. make cleanup-npm (prompts)
	$(PY) migrate.py cleanup --type $*

clean:  ## Remove the local staging cache and generated outputs (NOT remote artifacts)
	rm -rf staging docs/simple
	rm -f manifests/*.log manifests/*.out manifests/state.json
	rm -f manifests/docker-inventory.json manifests/npm-inventory.json manifests/data-inventory.json

distclean: clean  ## clean, and also drop the tracked PyPI inventory snapshot
	rm -f manifests/pypi-inventory.json
