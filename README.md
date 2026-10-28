# Artifactory → GitHub migration

Tooling to transfer IDM's artifacts from Artifactory (`packages.idmod.org`) into this
GitHub repo (`ZDu-IDM/artifact_transition`). See
[`artifactory-package-summary.md`](artifactory-package-summary.md) for the source inventory.

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
| `docker-production-public` + `idm-docker-public` + `docker-staging-public` (Docker) | 132 images | **ghcr.io** `ghcr.io/zdu-idm/<repo>/<image>` |
| `idm-npm-production` (Npm) | packages discovered live | **GitHub Packages npm** `@zdu-idm/*` |
| `idm-data` (Generic) | 568 files / 11 folders | **GitHub Releases** (one per folder) + `manifest.json` |

> The earlier `artifactory-package-summary.md` was a point-in-time snapshot and under-counted
> (e.g. it omitted `docker-staging-public`'s ~65 images). The toolkit now relies on **live
> discovery** instead; run `make discover` to see the authoritative coverage.

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
export ARTIFACTORY_USER=<username>          # optional if token is a bearer

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
target, inventory counts, publish progress, npm renames, data sizes, and split files. It's
read-only and safe to run anytime (before publishing it just shows 0 published):

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
