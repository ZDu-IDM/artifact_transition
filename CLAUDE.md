# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A one-shot migration toolkit that moves IDM's artifacts out of Artifactory
(`packages.idmod.org`) into GitHub, driven by `migrate.py` + the `artmig/` package and
configured by `migration.yaml`. Four artifact **types** are handled, each to a different
GitHub destination:

| Type  | Source (Artifactory LOCAL repo)        | GitHub destination |
|-------|----------------------------------------|--------------------|
| pypi  | `idm-pypi-production`                   | GitHub Releases + a static PEP 503 index under `docs/simple/` (served via Pages) |
| docker| `docker-production-public`, `idm-docker-public` (`docker-staging-public` excluded via `excluded_repos`) | `ghcr.io/<namespace>/<source-repo>/<image>:<tag>` |
| npm   | `idm-npm-production`                    | GitHub Packages npm, **rescoped** to `@<namespace>/<pkg>` |
| data  | `idm-data`                             | GitHub Releases (one per folder) + a per-release `manifest.json` |

Current status lives in `README.md` ("Migration status"): npm/data/pypi are done; docker is
paused at a 1-image pilot pending a team decision. The README is the authoritative operator
guide — read it for run procedures, consumer-pull instructions, and the "Lessons learned"
log of real bugs already fixed.

## Commands

```bash
make install          # pip install -r requirements.txt
make lint             # ruff check artmig migrate.py   (same as CI; `pip install ruff` first)
make check            # byte-compile sanity check (python -m compileall)
make auth-check       # report whether ARTIFACTORY_TOKEN / gh / docker are ready
make install-gui      # install the desktop console's deps (PySide6; CLI doesn't need it)
make gui              # launch the PySide6 migration console (python -m artmig.gui)
```

There is **no test suite** — CI (`.github/workflows/ci.yml`) runs only `ruff` + a
compile check on push/PR to `main`. "Testing" a change means a `--dry-run` (logs every
mutating action without doing it) and/or the read-only `inventory`/`verify`/`report`/`discover`
phases, none of which touch remote state.

### Running the pipeline

Everything goes through `python migrate.py <phase> --type <type>` (or the Makefile wrappers).
Phases: `inventory | download | transform | publish | verify | run | cleanup | report | discover`.
Types: `pypi | docker | npm | data | all`.

```bash
python migrate.py inventory --type all      # read-only crawl -> manifests/<type>-inventory.json
python migrate.py run --type pypi --dry-run # preview the whole pipeline, no changes
python migrate.py publish --type npm        # download -> transform -> publish, one type
python migrate.py verify --type all         # probe deployed artifacts; non-zero exit on failure
python migrate.py discover                  # live coverage report + gate (see below)
```

`publish` expands to `download -> transform -> publish`; `run` is the full
`inventory -> download -> transform -> publish -> verify` sequence (and, unlike other phases,
keeps going when one type fails). Any run can be narrowed to a subset of items with
`--only <id,id>` (exact-match package names, `<repo>/<image>` docker keys, or data folders;
`--image` is a kept alias).

### Credentials (never stored in the repo — read from env)

- `ARTIFACTORY_TOKEN` — API key / identity token; required for byte downloads and the Docker catalog.
- `ARTIFACTORY_USER` — required specifically for Docker registry login; optional for pypi/npm/data REST.
- GitHub auth via `gh auth login` or `GH_TOKEN`; ghcr needs a `docker login ghcr.io`.

`artmig/config.py` resolves these with `or` (not dict defaults) so an **empty** env var — what
GitHub Actions injects for an *unset* secret — falls back to config/`None` instead of blanking it.

## Architecture

### The type-module contract

`migrate.py` is a thin dispatcher. For each selected type it imports one module from
`artmig/` (`MODULES = {pypi, docker, npm, data}`) and calls a **phase function of a fixed name**
on it: `inventory(ctx) -> dict`, then `download/transform/publish/verify/cleanup(ctx) -> None`.
Adding behavior means editing the relevant type module; adding a type means writing a module that
implements these functions and registering it in `MODULES`. There is no base class — the modules
are duck-typed by this function-name convention.

### Context is the single dependency carrier

Every phase receives one `Context` (`artmig/context.py`) bundling the shared collaborators:
`cfg` (config), `art` (`Artifactory` read client), `gh` (`GitHub` CLI wrapper), `state`
(resume ledger), `dry_run`, and `item_filter`. `Context` also owns inventory JSON I/O
(`read_inventory`/`write_inventory`) and staging paths. Type modules never construct these
themselves — they pull everything off `ctx`.

### Two-stage data flow: inventory JSON, then state ledger

1. `inventory(ctx)` crawls Artifactory and returns a dict that the CLI writes to
   `manifests/<type>-inventory.json`. This is the **immutable plan** — all later phases read it
   (`ctx.read_inventory`) and never re-crawl. **All four `*-inventory.json` and `state.json` are
   git-tracked** (`.gitignore` keeps them; only logs are ignored), so a fresh checkout — and the
   GUI — see real status without re-crawling. The `migrate.yml` workflow commits them back after
   any run that changes them.
2. `manifests/state.json` (`artmig/state.py`) is the **resume ledger**: per-item status keyed by
   a stable id like `pypi:emodpy:emodpy-2.1.16.whl`. Every phase checks `state.is_done(key, ...)`
   and skips finished work, so an interrupted migration resumes cleanly and reruns are cheap.
   `cleanup` deletes remote artifacts *and* `state.clear(key)`s them so a later `publish` re-runs.

**Idempotency/resumability is a hard requirement of this codebase** — these are large, slow,
rate-limited, failure-prone network operations. New code in any phase must skip already-done work
(via `state` and/or by probing the destination, e.g. `gh.existing_asset_names`, "skip tags already
on ghcr") rather than redoing it.

### Clients

- `artmig/artifactory.py` — authenticated **read-only** REST + Docker-v2 client (storage walk,
  PyPI simple index, npm metadata, docker catalog/tags, streamed checksummed download). Docker
  uses the per-repo subdomain endpoint (`<registry>/artifactory/api/docker/<repo>/v2/...`); the
  bare `/v2/` root is misconfigured upstream. Downloads write to a `.part` temp then atomically
  `replace`.
- `artmig/github.py` — shells out to the **`gh` CLI** (not the REST API) so auth piggybacks on
  `gh auth login`. Uploads skip existing assets and self-pace against GitHub's secondary
  (content-creation) rate limit with escalating backoff. `delete_package` failures are warnings,
  not errors, so a partial rollback continues.

### Desktop console (`artmig/gui.py`)

A PySide6 GUI (`make gui`) for monitoring + running migrations. It **reuses the type modules**,
never reimplements migration logic: status is rolled up read-only from the committed `state.json`
+ inventories (no network to render), and clicking **Migrate** runs `download → transform →
publish` for one item in a background `QThread` by setting `ctx.item_filter = [<id>]` — the same
scoping the CLI exposes as `--only`. The `artmig` logger is bridged to the activity pane. PySide6
is an optional extra (`requirements-gui.txt`), so the CLI stays dependency-light. When editing it,
keep logic in the type modules and drive via `ctx.item_filter`.

### Cross-cutting helpers (`artmig/util.py`)

Shared across modules: `retry()` (linear backoff with a `give_up` predicate — 4xx except 429 are
non-retryable), `sha256_file`/`sha256_url`, `normalize_pypi_name` (PEP 503), `sanitize_asset_name`
(GitHub assets can't contain `/`, so `a/b/c.bin -> a__b__c.bin`, reversibly, recorded in a
manifest), `split_file`/`plan_split` (files over the 2 GiB asset cap are split into `.partNNN`),
`resolve_exe` (finds `npm.cmd`/`docker.exe` on Windows), and the `verify` probe helpers
(`probe_asset`, `http_reachable`, `report_probes` — which raises on any hard FAIL).

### Safety rails

- **Destructive guard** (`migrate.py:_guard_destructive`): a real (non-dry-run) `publish`/`run
  --type all`, and *any* real `cleanup`, prompt for a typed `yes`. `--yes` bypasses (used in CI,
  which can't prompt). Single-type publishes and all dry-runs are unguarded.
- **Coverage gate** (`discover`): audits the live `GET /api/repositories` LOCAL set against
  `migration.yaml` and **fails if any LOCAL repo is unhandled**, so nothing is silently missed as
  Artifactory drifts. Only LOCAL (IDM-owned) repos are in scope; REMOTE (caching proxies) and
  VIRTUAL (aggregate views) are excluded by design.
- `verify` actively probes deployed artifacts (HTTP reachability, sha256, `npm view`,
  `docker manifest inspect`) and exits non-zero on failure; `SKIP` means a missing prerequisite,
  not a failure.

## Conventions

- Target **Python 3.9** (`ruff.toml` `target-version = "py39"`), but files use
  `from __future__ import annotations` so `X | None` syntax is fine. Line length 100. Lint rules:
  `E, F, W, I, B, UP`. Match the existing style — module docstring explaining the strategy, a
  `TYPE = "<type>"` constant, `log = get_logger()`, section-comment banners (`# ---- download ----`).
- Logging only via `get_logger()` (named `artmig`); the CLI tees to `manifests/migration-*.log`.
  Dry-run paths log `[dry-run] <action>` instead of mutating.
- `migration.yaml` holds everything configurable (owner/repo, namespace, source repos,
  `expected_count` reconciliation hints, `tag_selection`, size caps) and **no secrets**.
