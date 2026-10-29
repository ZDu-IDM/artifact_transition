# Artifactory → GitHub migration

Tooling to transfer IDM's artifacts from Artifactory (`packages.idmod.org`) into this
GitHub repo (`ZDu-IDM/artifact_transition`). See
[`artifactory-package-summary.md`](artifactory-package-summary.md) for the original (stale)
source snapshot; `make discover` produces the current live inventory.

## Migration status (last updated 2026-10-29)

| Type | Status | Result |
|---|---|---|
| **npm** | ✅ **done** | 4 packages / 15 versions → `@zdu-idm/*` on GitHub Packages (verified) |
| **data** | ✅ **done** | 11 folder-releases / 578 assets, ~11.4 GB (sha256-verified) |
| **pypi** | ✅ **done** | 91 releases / 1,692 assets, PEP 503 index live & `pip`-installable |
| **docker** | 🟡 **pilot only** | 1 image (`idmtools/container-rocky-runtime`, 7 tags) migrated; full run pending (175 images / 569 tags / **281 GB**) |

**Work is paused pending a team decision on the Docker run** (see
[Resuming the Docker migration](#resuming-the-docker-migration)). npm/data/pypi are complete; nothing
is in flight. The toolkit is fully built, CI-linted, and exercised end-to-end from the workflow.

## Scope: all LOCAL (IDM-owned) repositories

The migration covers **every LOCAL repository** — the ones that store IDM's own artifacts —
discovered live from `GET /api/repositories` (not a static list). REMOTE repos are caching
proxies of public registries (PyPI, npm, Docker Hub, CRAN, NuGet, RPM) and VIRTUAL repos are
aggregate views — neither holds IDM-owned data, so both are excluded. `migrate.py discover`
audits the live LOCAL set against the config and **fails if any LOCAL repo is unhandled**, so
nothing can be silently missed as Artifactory changes.

Current LOCAL repos → GitHub target:

| Source (LOCAL) | Volume | GitHub target |
|---|---|---|
| `idm-pypi-production` (Pypi) | 91 pkgs / 1,692 files | **GitHub Releases** + **PEP 503 index** on GitHub Pages |
| `docker-production-public` + `idm-docker-public` + `docker-staging-public` (Docker) | 175 images / 569 tags / ~281 GB (authenticated count) | **ghcr.io** `ghcr.io/zdu-idm/<repo>/<image>` |
| `idm-npm-production` (Npm) | packages discovered live | **GitHub Packages npm** `@zdu-idm/*` |
| `idm-data` (Generic) | 568 files / 11 folders | **GitHub Releases** (one per folder) + `manifest.json` |

> The earlier `artifactory-package-summary.{md,html,pdf}` was a point-in-time snapshot and
> under-counted (e.g. it omitted `docker-staging-public`'s ~65 images). `make discover` replaces
> it with a **live** equivalent: it writes `report/discovery-report.{md,html,pdf}` (same three
> formats) documenting the current instance structure + coverage, and **fails** if any LOCAL repo
> is unhandled.

**Docker tag scope:** by default all tags migrate. Set `types.docker.tag_selection` in
`migration.yaml` to `one` (one representative tag per image — `latest` if present, else the first;
keeps every image) or `latest` (only `latest`; note only 66/175 images have it, so 109 would be
skipped) for a bounded pass.

GitHub has no native PyPI registry, so Python wheels/sdists become **release assets** and a
static **PEP 503 "simple" index** (`docs/simple/`, served via GitHub Pages) makes them
`pip install`-able. The 568 data files aren't packages, so they also become release assets,
with nested paths flattened reversibly (`a/b/c.bin` → `a__b__c.bin`) and recorded in a
per-release `manifest.json`.

## Setup

```bash
pip install -r requirements.txt

# Artifactory read credentials (needed for byte downloads + Docker catalog)
export ARTIFACTORY_TOKEN=<api-key-or-identity-token>
export ARTIFACTORY_USER=<username>          # REQUIRED for Docker login; optional for pypi/npm/data REST downloads

# GitHub auth (releases, ghcr, npm)
gh auth login
echo $GH_TOKEN | docker login ghcr.io -u <you> --password-stdin
```

Configuration lives in [`migration.yaml`](migration.yaml) — owner/repo, namespace, source
repos, and the expected counts used for reconciliation. **No secrets are stored there.**

## Usage

The pipeline has five idempotent, resumable phases. **Always start with a read-only
`inventory` and reconcile counts before pushing anything.**

```bash
# 1. Read-only: crawl Artifactory, write manifests/<type>-inventory.json, check counts
python migrate.py inventory --type all

# 2. Dry-run the rest (logs every mutating action without doing it)
python migrate.py run --type pypi --dry-run

# 3. Execute per type (download → transform → publish)
python migrate.py publish --type npm
python migrate.py publish --type pypi
python migrate.py publish --type data
python migrate.py publish --type docker

# 4. Probe the deployed artifacts (exits non-zero if a hard check fails)
python migrate.py verify --type all
```

A real (non-dry-run) `publish`/`run --type all` pushes to **all** registries at once and is
hard to undo, so it requires confirmation — type `yes` at the prompt, or pass `--yes` to
bypass (e.g. in CI). Single-type publishes and any `--dry-run` are never guarded.

Phases: `inventory | download | transform | publish | verify | run`.
Re-running skips items already recorded as done in `manifests/state.json`, so an interrupted
migration resumes cleanly.

`verify` actively **probes the deployed artifacts** (it doesn't just print commands): the PyPI
Pages index + a release asset (sha256-checked), a `data/<folder>` asset + its manifest, an npm
package via `npm view`, and a ghcr image via `docker manifest inspect`. It reports `OK/FAIL/SKIP`
per probe and exits non-zero on any hard failure. `SKIP` means a prerequisite is missing (e.g. npm
auth, or Docker tags that need an authenticated inventory), not a failure.

### Migration report

`report` summarizes the migration from the inventories + `state.json` — per-type source,
target, inventory counts, **estimated storage size**, publish progress, npm renames, and split
files. The size column answers "how much space will the real publish need", with a grand total.
Sizes come from the one-call File List API (needs `ARTIFACTORY_TOKEN`); without a token, `data`
and `npm` sizes still resolve (from the inventory / tarball HEADs) and `pypi`/`docker` show
`n/a (needs token)`. It's read-only and safe to run anytime (before publishing it shows 0 published):

```bash
python migrate.py report        # writes report/migration-report.{html,body.html,md}
make report
```

`migration-report.html` is a standalone page; `migration-report.body.html` is a head/body-less
fragment for embedding; `migration-report.md` mirrors the original summary style. To serve the
report on Pages, copy the HTML into `docs/`.

### Rollback / cleanup

`cleanup` **undoes a migration** by deleting what it published — PyPI/data releases (via
`gh release delete --cleanup-tag`) and npm/ghcr packages (via the GitHub Packages API) — and
clears the matching `state.json` entries so a later `publish` re-runs cleanly.

```bash
python migrate.py cleanup --type npm --dry-run   # preview deletions, remove nothing
python migrate.py cleanup --type npm             # real: prompts for 'yes'
python migrate.py cleanup --type all --yes       # roll back everything, no prompt
```

Because it is **destructive and not recoverable**, any real `cleanup` (even single-type) requires
confirmation — type `yes`, or pass `--yes`. Deleting packages needs a token with
`delete:packages`; missing packages/permissions are logged as warnings, not hard failures. Note
`make clean` (local cache) is unrelated to `cleanup`/`make cleanup` (remote artifacts).

### Running the migration from GitHub Actions

`.github/workflows/migrate.yml` runs any phase from CI — **manual trigger only**
(Actions → "Run migration" → pick `phase`, `type`, and `dry_run`). It authenticates to Artifactory
and the GitHub registries from secrets, passes `--yes` for real runs (the interactive guard can't
prompt in CI), always generates the report, and uploads `manifests/` + `report/` as a run artifact.

Required/optional repo secrets:

| Secret | Needed for |
|---|---|
| `ARTIFACTORY_TOKEN` | downloads + Docker catalog/tags (required for real runs) |
| `ARTIFACTORY_USER` | Artifactory basic auth (optional if the token is a bearer) |
| `ARTIFACTORY_URL` | override the registry URL (optional) |
| `GH_PAT` | a PAT with `write:packages` (+ `delete:packages` for `cleanup`); falls back to the built-in `GITHUB_TOKEN` otherwise |

Start with `dry_run: true`. Mind runner limits: the `data` type is ~12 GB and may exceed the
hosted runner's disk — migrate it locally or on a larger runner. Publishing/deleting npm & ghcr
packages needs the `packages: write` permission (already set in the workflow).

### Make targets

A `Makefile` wraps the common operations (`make help` lists them all):

```bash
make install                 # pip install -r requirements.txt
make auth-check              # report whether ARTIFACTORY_TOKEN / gh / docker are ready
make inventory TYPE=all      # crawl Artifactory (read-only)
make dry-run TYPE=pypi       # preview the full pipeline, no changes
make dryrun-npm              # dry-run publish a single type
make publish-npm             # publish a single type
make index                   # regenerate the PEP 503 index
make clean                   # drop staging cache + generated outputs
```

Most targets accept `TYPE=all|pypi|docker|npm|data` (default `all`); per-type shortcuts like
`make publish-docker` are also available.

### Linting / CI

`.github/workflows/ci.yml` runs `ruff` (config in `ruff.toml`) plus a byte-compile check on every
push to `main` and every PR. Run it locally with `make lint` (`pip install ruff` first) or
`ruff check artmig migrate.py`.

## After migration — how consumers pull

```bash
# Python (PEP 503 index on GitHub Pages)
pip install --index-url https://zdu-idm.github.io/artifact_transition/simple/ emodpy

# Docker
docker pull ghcr.io/zdu-idm/idm-docker-public/emodpy:<tag>

# npm (note the scope change: <pkg> → @zdu-idm/<pkg>)
npm install @zdu-idm/comps-ui-auth --registry=https://npm.pkg.github.com
```

npm packages are **renamed** on GitHub (scope must match the owner). Old → new:
`comps-ui-auth → @zdu-idm/comps-ui-auth`, `microcomps → @zdu-idm/microcomps`,
`postette → @zdu-idm/postette`, `comps-ui → @zdu-idm/comps-ui`.

Data files: open the `data/<folder>` release, read its `manifest.json` for the
original-path → asset-name map, and download the flattened asset.

**Files over the 2 GiB cap** are split into parts (`<asset>.part000`, `.part001`, …).
The manifest entry is marked `"split": true` with per-part checksums and a reassembly
command. To rebuild:

```bash
# Unix
cat malaria__jb_kyle_malaria_octave.tgz.part* > jb_kyle_malaria_octave.tgz
# Windows
copy /b malaria__jb_kyle_malaria_octave.tgz.part* jb_kyle_malaria_octave.tgz
# then verify against the whole-file sha256 in manifest.json
```

## Layout

```
migrate.py          CLI entrypoint
artmig/             package: config, artifactory client, github wrapper, state, util,
                    + one module per type (pypi, docker, npm, data)
migration.yaml      configuration (no secrets)
manifests/          generated inventory JSON + resume state + logs
staging/            local download cache (gitignored)
docs/simple/        generated PEP 503 index (GitHub Pages source)
```

## Notes & limits

- Release assets are capped at **2 GiB**; larger source files are auto-split into ~1.9 GB parts
  (`split_chunk_bytes`) and reassembled from the manifest. One such file exists today
  (`malaria/jb_kyle_malaria_octave.tgz`, 2.63 GB).
- Docker (67 images × many tags) and `.bin` climate rasters are large — watch local disk,
  ghcr storage, and GitHub API rate limits. The run is resumable if interrupted.
- The summary is a point-in-time snapshot; `inventory` re-counts live and warns on drift
  (e.g. PyPI shows 91 normalized names vs ~107 raw names — expected due to PEP 503).
## GitHub Pages (PyPI index)

The PEP 503 index is served at `https://zdu-idm.github.io/artifact_transition/simple/` by the
[`pages.yml`](.github/workflows/pages.yml) workflow. On every push to `main` that touches
`manifests/pypi-inventory.json` (or the generator), CI regenerates `docs/simple/` from the
committed inventory snapshot and deploys `docs/` — **no Artifactory credentials needed**, since
`transform` is fully offline.

One-time setup: **Settings → Pages → Build and deployment → Source: GitHub Actions**
(or `gh api -X POST repos/ZDu-IDM/artifact_transition/pages -f build_type=workflow`).

To refresh the index after a new migration: re-run `migrate.py inventory --type pypi`, commit the
updated `manifests/pypi-inventory.json`, and push — the workflow redeploys automatically.

## Resuming the Docker migration

npm/data/pypi are done. Docker is the only remaining type. One image was migrated as a
pilot; the full run is **175 images / 569 tags / ~281 GB**.

**Decisions to settle with the team first:**
1. **Tag scope** — all 569 tags, or `tag_selection: one` (one representative tag per image →
   ~175 tags, all images kept). Note tags of an image share layers, so fewer tags mainly cuts
   push/manifest operations, not necessarily total bytes.
2. **Runner** — a hosted GitHub runner has ~14 GB disk and a 6 h job limit. The toolkit is now
   **disk-bounded** (`docker rmi` after each image) and **resumable** (skips tags already on
   ghcr), so it can run as **repeated hosted jobs** that each make progress, *or* on a
   **self-hosted / large runner** in fewer passes. ~281 GB at the observed throughput is several
   hours.
3. **Visibility** — ghcr packages published via `GITHUB_TOKEN` start **private**. Decide whether
   images should be made **public** (anonymous `docker pull`, matching old Artifactory behavior).
   A post-publish auto-public step is noted as a future enhancement, not yet implemented.

**Prerequisites (already satisfied):** `ARTIFACTORY_TOKEN` + `ARTIFACTORY_USER` secrets set
(Docker login requires the username); ghcr login handled by the workflow.

**To run it** (Actions → "Run migration", or `gh workflow run migrate.yml`):

```bash
# optional: set a bounded scope first in migration.yaml -> types.docker.tag_selection: one
# full run (repeat until it reports everything already on ghcr):
gh workflow run migrate.yml --ref main -f phase=publish -f type=docker -f dry_run=false

# a single image / subset (what the pilot used):
gh workflow run migrate.yml --ref main -f phase=publish -f type=docker -f dry_run=false \
  -f image=idmtools/container-rocky-runtime

# verify afterwards:
gh workflow run migrate.yml --ref main -f phase=verify -f type=docker -f dry_run=false
```

Because publish skips tags already on ghcr, re-dispatching after a timeout/partial run simply
continues. Confirm completeness any time with `make discover` and a fresh `report`.

## Lessons learned (issues found & fixed during the real migrations)

Captured for the record — each was discovered by running the real publishes and is now handled:

- **Stale summary** — the live instance had drifted (e.g. `docker-staging-public`'s ~65 images
  were missing from the old summary). Fixed by live discovery + a coverage gate (`discover`).
- **npm hardcoded list** → replaced with live package discovery.
- **npm irregular tarball URL** (`comps-ui-auth@2.1.0` lacked the `/-/` path) → download via the
  authoritative `dist.tarball` URL.
- **npm version ordering** — publishing versions out of order tripped npm's "can't move `latest`
  backward" → publish under a non-`latest` tag, then set `latest` explicitly; skip duplicates.
- **GitHub 0-byte assets rejected** (`400 Bad Content-Length`) — 2 empty data files recorded in
  the manifest (`"empty": true`) and skipped.
- **GitHub secondary rate limit** on bulk release-asset uploads — uploads are now resumable
  (skip existing) + batched with backoff.
- **Docker registry = subdomain method** — `packages.idmod.org/v2/` root is misconfigured (400);
  the real host is `<repo>.packages.idmod.org`. Docker login also requires `ARTIFACTORY_USER`
  (REST downloads only needed the token).
- **Empty env vars from unset secrets** — GitHub injects `""` for an unset secret; config now
  falls back to defaults/`None` instead of blanking values.
- **CI runner limits** — disk and 6 h job cap drove the disk-bounded + resumable Docker design.
