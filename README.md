# Artifactory → GitHub migration

Tooling to transfer IDM's artifacts from Artifactory (`packages.idmod.org`) into this
GitHub repo (`ZDu-IDM/artifact_transition`). See
[`artifactory-package-summary.md`](artifactory-package-summary.md) for the source inventory.

## What moves where

| Source (Artifactory) | Volume | GitHub target |
|---|---|---|
| `idm-pypi-production` (Python) | ~107 names (91 after PEP 503 dedup) | **GitHub Releases** + **PEP 503 index** on GitHub Pages |
| `docker-production-public`, `idm-docker-public` | 67 images | **ghcr.io** `ghcr.io/zdu-idm/<repo>/<image>` |
| `idm-npm-production` (npm) | 4 packages | **GitHub Packages npm** `@zdu-idm/*` |
| `idm-data` (generic data) | 568 files / 11 folders | **GitHub Releases** (one per folder) + `manifest.json` |
| NuGet / CRAN / RPM | empty | not migrated |

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

# 4. Print verification commands
python migrate.py verify --type all
```

Phases: `inventory | download | transform | publish | verify | run`.
Re-running skips items already recorded as done in `manifests/state.json`, so an interrupted
migration resumes cleanly.

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
