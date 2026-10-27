# Artifactory Package Summary — packages.idmod.org

*Generated: 2026-07-14 · Source: https://packages.idmod.org*

## Instance structure

There are **32 repositories** spanning **7 package types**, organized as LOCAL (hosted) / REMOTE (proxy) / VIRTUAL (aggregate). Only the **LOCAL production** repos actually store IDM-owned artifacts.

- **LOCAL** — org-hosted artifacts (IDM's own packages land here)
- **REMOTE** — caching proxies of public registries (PyPI, npm, Docker Hub, CRAN, NuGet, RPM)
- **VIRTUAL** — a single URL aggregating a local + remote pair

| Package type | Production LOCAL repo | Contents? | Latest activity |
|---|---|---|---|
| **Python (PyPI)** | `idm-pypi-production` | ✅ ~107 package names | 2026-06-23 |
| **Docker** | `docker-production-public` (51 images), `idm-docker-public` (16) | ✅ 67 image repos | 2026-06-27 |
| **npm** | `idm-npm-production` | ✅ 4 packages | 2026-06-16 |
| **Generic (data)** | `idm-data` | ✅ data files, not packages | 2026-07-12 |
| **NuGet** | `idm-nuget-production` | ⬜ empty | — |
| **CRAN (R)** | `idm-cran-production` | ⬜ empty | — |
| **YUM/RPM** | `idm-rpm-production` | ⬜ empty | — |

> NuGet, CRAN, and RPM production repos are provisioned but currently hold **no packages** — only their remote proxies (`nuget-remote`, `cran-remote`, `rpm-remote`) are in use. Also present: 2 `testteam-*` dev/stage repos (Docker + PyPI) and Docker staging (`docker-staging-public`).

## Python — `idm-pypi-production` (~107 packages)

The largest and most active repository. Flagship packages with latest version / release date / publisher:

| Package | Latest version | Release date | Published by (owner) |
|---|---|---|---|
| `emodpy` | 2.1.16 | 2025-12-04 | pypi-prod-deploy (CI) |
| `emodpy-hiv` | 3.0.1 | 2026-03-03 | pypi-prod-deploy (CI) |
| `emodpy-malaria` | 4.3.2 | 2026-01-22 | idm_bamboo_user (CI) |
| `emod-api` | 1.33.7 | 2026-01-22 | artifact_staging_deploy (CI) |
| `emod-hiv` | 1.0.5 | 2025-10-22 | stitova |
| `emod-malaria` | 0.0.78 | 2025-10-21 | stitova |
| `idmtools` | 2.2.2 | 2026-04-22 | idm_bamboo_user (CI) |
| `idmtools-platform-comps` | 2.2.2 | 2026-04-22 | idm_bamboo_user (CI) |
| `laser-core` | 0.7.0 | 2025-11-11 | jbloedow |
| `pyCOMPS` | 2.11.0 | 2024-06-20 | jsteinkraus |
| `idm-buildtools` | 1.0.5 | 2024-02-28 | idm_bamboo_user (CI) |
| `dtk-tools` | 1.2.0rc6 | 2021-03-18 | zdu (legacy) |

**Package families in this repo:**

- **EMOD / emodpy** disease models — `emod-{api,common,hiv,malaria,measles,polio,tbhiv,typhoid}`, `emodpy-*`
- **idmtools** platform suite — `idmtools-platform-{comps,container,slurm,local,general}`, `idmtools-cli`, `idmtools-models`
- **LASER** agent-based framework — `laser-core`, `laser_polio`, `laser-*-model`, `idmlaser*`
- **Utilities** — `pyCOMPS`, `idm-test`, `idm-buildtools`, `emod-api`
- **Vendored scientific deps** — `GDAL`, `Fiona`, `Shapely`, `geopandas`, `pyproj`, `statsmodels`, `lz4`, `scikit-cuda`

> Note: many names appear twice (e.g. `idmtools_cli` vs `idmtools-cli`) due to PEP 503 name normalization.

## Docker — 67 image repositories

- **`docker-production-public`** (51 images): build/test environments and model runtimes — `emodpy`, `idmtools` (SSMT workers), `dtk-alma-*`, `dtk-rocky-*`, `idm-rocky-py39*`, `r-apache-base`, `laser*`, plus many user-scoped dev images (e.g. `krosenfeld_*`, `jonathanhhb-*`, `cwiswell-*`).
- **`idm-docker-public`** (16 images): `dtk`, `dtk-tools`, `emod`, `emodpy`, `idmtools`, `idmtools-slurm`, and org-partner images (`northwestern`, `nyu`).
- Latest tags observed: `idmtools/comps_ssmt_worker:2.1.0.11` (2025-03-31), `idm/dtk-rocky-testenv` (2023-10-26).

*(Owner/tag detail is limited here because the Docker v2 `_catalog` API requires auth; the above is reconstructed from the storage-listing API.)*

## npm — `idm-npm-production` (4 packages, full coverage)

| Package | Latest version | Last modified | Notes |
|---|---|---|---|
| `comps-ui-auth` | 2.1.1 | 2022-09-30 | COMPS UI auth lib |
| `microcomps` | 0.1.0-build.42 | 2022-11-09 | |
| `postette` | 0.4.1 | 2021-02-09 | |
| `comps-ui` | 0.2.0 | 2019-01-22 | oldest |

These are UI components for the COMPS platform; all are relatively dormant (last activity 2022).

## Generic — `idm-data`

Not software packages but **data assets** — **568 data files across 11 project folders**. This is the **most recently active** repo (2026-07-12); model input/data files are updated more frequently than released packages. Content is dominated by EMOD/DTK simulation inputs.

### Data types by file format

| Extension | Count | What it is |
|---|---|---|
| `.json` | 345 | DTK/EMOD **demographics & config** files (population, node properties, simulation setup) |
| `.bin` | 173 | EMOD **binary climate/spatial inputs** (air & land temperature, rainfall, humidity rasters) — each paired with a `.bin.json` header |
| `.txt` | 11 | Notes / manifests / plain-text inputs |
| `.gz` | 9 | Gzip-compressed datasets |
| `.tgz` | 7 | Tarball data bundles (HIV, Malaria project packs) |
| `.pkl` | 6 | Python **pickle** objects (MEASLES — serialized model state/data) |
| `.zip` | 5 | Zipped archives (LASER data, GADM shapefiles) |
| `.csv` | 5 | Tabular data (LASER, tb) |
| `.npy` | 3 | **NumPy** array binaries (LASER matrices) |
| `.tar` | 1 | Tar archive (oc-proj) |
| `.exe` | 1 | Executable (LASER) |
| `.conf` | 1 | Config file (general) |
| *(no ext)* | 1 | Uncategorized |

### Organized by project folder

| Folder | Files | Data types / character |
|---|---|---|
| `DtkInput` | 524 | Bulk of the repo — EMOD/DTK **simulation inputs**: `.json` (demographics/config) + `.bin` (climate rasters) + `.txt` |
| `LASER` | 14 | Mixed LASER model assets: `.gz`, `.npy`, `.bin`, `.zip`, `.txt`, `.exe` |
| `laser` | 10 | LASER datasets: `.gz`, `.csv`, `.npy` |
| `MEASLES` | 7 | `.pkl` (pickled Python data) + `.gz` |
| `gadm` | 3 | `.zip` — **GADM administrative-boundary** shapefiles (geographic) |
| `Malaria` | 3 | `.tgz` data bundles |
| `HIV` | 2 | `.tgz` data bundles |
| `oc-proj` | 2 | `.json` + `.tar` |
| `tb` | 1 | `.csv` |
| `malaria` | 1 | `.tgz` |
| `general` | 1 | `.conf` |

> The dominant `.json` + `.bin` pairing is the EMOD signature: climate inputs ship as a raw binary raster (`.bin`) plus a JSON sidecar describing dimensions, node IDs, and date ranges. Note both `LASER`/`laser` and `Malaria`/`malaria` exist as separate folders — data uploaded by different teams/scripts over time.

## Bottom line

IDM's Artifactory is predominantly a **Python + Docker** shop:

- **PyPI** (~107 packages) centered on the EMOD/emodpy, idmtools, and LASER ecosystems
- **Docker** (67 images) for build environments and model runtimes
- **npm** holds 4 legacy COMPS UI libraries
- **`idm-data`** holds project datasets (most recently active)
- **NuGet / CRAN / RPM** production repos are empty placeholders

Most current releases are pushed by CI service accounts (`idm_bamboo_user`, `pypi-prod-deploy`, `artifact_staging_deploy`), with some manual publishes by individual engineers.

---

## Methodology / data sources

Data was collected from the public Artifactory REST API:

- Repository inventory: `GET /artifactory/api/repositories`
- PyPI package list: `GET /artifactory/api/pypi/idm-pypi-production/simple/`
- Folder/artifact listings: `GET /artifactory/api/storage/{repo}/`
- Latest activity + owner: `GET /artifactory/api/storage/{repo}/{path}?lastModified` (`createdBy` field)
- npm metadata: `GET /artifactory/api/npm/idm-npm-production/{package}`

Note: AQL (`/api/search/aql`) and the Docker v2 registry catalog (`/v2/_catalog`) require authentication and were not used.
